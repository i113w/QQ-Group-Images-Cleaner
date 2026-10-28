"""Read-only Windows metadata APIs. No Everything SDK or driver is used.

GetFileInformationByHandleEx returns directory entries in 64 KiB batches,
including size and all three timestamps, without a stat/open call per file.
The USN journal is optional; directory notifications work without elevation.
"""

import ctypes
from ctypes import wintypes
import os
import struct
import sys
import threading

from lib.file_metadata import DirectoryListing, DirectoryRecord, FileRecord


IS_WINDOWS = sys.platform == 'win32'
INVALID_HANDLE = ctypes.c_void_p(-1).value
FILE_ATTRIBUTE_DIRECTORY = 0x10
FILE_ATTRIBUTE_REPARSE_POINT = 0x400
FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
FILE_FLAG_OVERLAPPED = 0x40000000
FILETIME_EPOCH = 116444736000000000


def long_path(path):
    path = os.path.abspath(path)
    if path.startswith('\\\\?\\'):
        return path
    if path.startswith('\\\\'):
        return '\\\\?\\UNC\\' + path[2:]
    return '\\\\?\\' + path


def filetime_ns(value):
    return max(0, (value - FILETIME_EPOCH) * 100) if value else 0


if IS_WINDOWS:
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateFileW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
                                  wintypes.HANDLE)
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel.CloseHandle.restype = wintypes.BOOL
    kernel.GetFileInformationByHandleEx.argtypes = (
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
    kernel.GetFileInformationByHandleEx.restype = wintypes.BOOL
    kernel.DeviceIoControl.argtypes = (
        wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
        ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p)
    kernel.DeviceIoControl.restype = wintypes.BOOL


class NativeUnavailable(OSError):
    pass


def _open_directory(path, overlapped=False):
    flags = FILE_FLAG_BACKUP_SEMANTICS | (FILE_FLAG_OVERLAPPED if overlapped else 0)
    handle = kernel.CreateFileW(long_path(path), 1, 7, None, 3, flags, None)
    if handle == INVALID_HANDLE:
        raise ctypes.WinError(ctypes.get_last_error())
    return handle


def enumerate_directory(path):
    """Enumerate a directory in native batches; do not follow reparse points."""
    if not IS_WINDOWS:
        raise NativeUnavailable('Windows directory APIs are unavailable.')
    before = os.stat(path, follow_symlinks=False)
    if getattr(before, 'st_file_attributes', 0) & FILE_ATTRIBUTE_REPARSE_POINT:
        raise OSError('Reparse points are excluded from indexing: ' + path)
    handle = _open_directory(path)
    buffer = ctypes.create_string_buffer(64 * 1024)
    files, children = [], []
    file_id = ''
    try:
        identity = ctypes.create_string_buffer(24)
        if kernel.GetFileInformationByHandleEx(handle, 18, identity, len(identity)):
            file_id = f'{int.from_bytes(identity.raw[8:24], "little"):032x}'
        info_class = 20  # FileIdExtdDirectoryRestartInfo
        while True:
            if not kernel.GetFileInformationByHandleEx(handle, info_class, buffer, len(buffer)):
                error = ctypes.get_last_error()
                if error == 18:  # ERROR_NO_MORE_FILES
                    break
                if info_class == 20 and error in (1, 50, 87):
                    raise NativeUnavailable(error, 'Bulk directory information is unsupported.')
                raise ctypes.WinError(error)
            info_class = 19  # FileIdExtdDirectoryInfo (continue the same enumeration)
            data = buffer.raw
            offset = 0
            while True:
                (next_offset, _, created, accessed, modified, _, size, _, attrs,
                 name_length, _, _, identity_bytes) = struct.unpack_from(
                    '<IIqqqqqqIIII16s', data, offset)
                if name_length > len(data) - offset - 88 or name_length % 2:
                    raise OSError('Invalid directory record returned by Windows.')
                name = data[offset + 88:offset + 88 + name_length].decode('utf-16-le')
                if name not in ('.', '..') and not attrs & FILE_ATTRIBUTE_REPARSE_POINT:
                    full_path = os.path.join(path, name)
                    if attrs & FILE_ATTRIBUTE_DIRECTORY:
                        children.append(DirectoryRecord(
                            full_path, filetime_ns(modified), filetime_ns(created),
                            f'{int.from_bytes(identity_bytes, "little"):032x}'))
                    else:
                        files.append(FileRecord(full_path, size, filetime_ns(created),
                                                filetime_ns(modified), filetime_ns(accessed)))
                if next_offset == 0:
                    break
                if next_offset < 88 or offset + next_offset >= len(data):
                    raise OSError('Invalid directory record offset returned by Windows.')
                offset += next_offset
    finally:
        kernel.CloseHandle(handle)
    # Save the fingerprint from BEFORE enumeration. A concurrent change makes
    # the next reconciliation dirty instead of being silently marked up to date.
    directory = DirectoryRecord(path, before.st_mtime_ns,
                                getattr(before, 'st_birthtime_ns', before.st_ctime_ns), file_id)
    return DirectoryListing(directory, files, children)


def parse_usn_records(data):
    """Parse documented V2/V3 records; reject unknown/truncated layouts."""
    records = []
    offset = 8  # leading next-USN value
    while offset < len(data):
        if offset + 8 > len(data):
            raise NativeUnavailable('Truncated USN record.')
        length, major, _ = struct.unpack_from('<IHH', data, offset)
        if length < 8 or offset + length > len(data):
            raise NativeUnavailable('Invalid USN record length.')
        if major == 2 and length >= 60:
            file_id, parent_id = struct.unpack_from('<QQ', data, offset + 8)
            reason = struct.unpack_from('<I', data, offset + 40)[0]
            attrs = struct.unpack_from('<I', data, offset + 52)[0]
            name_length, name_offset = struct.unpack_from('<HH', data, offset + 56)
        elif major == 3 and length >= 76:
            file_id = int.from_bytes(data[offset + 8:offset + 24], 'little')
            parent_id = int.from_bytes(data[offset + 24:offset + 40], 'little')
            reason = struct.unpack_from('<I', data, offset + 56)[0]
            attrs = struct.unpack_from('<I', data, offset + 68)[0]
            name_length, name_offset = struct.unpack_from('<HH', data, offset + 72)
        else:
            raise NativeUnavailable(f'Unsupported USN record version: {major}.')
        minimum_offset = 60 if major == 2 else 76
        if name_offset < minimum_offset or name_offset + name_length > length or name_length % 2:
            raise NativeUnavailable('Invalid USN filename bounds.')
        name = data[offset + name_offset:offset + name_offset + name_length].decode('utf-16-le')
        records.append((f'{file_id:032x}', f'{parent_id:032x}', name, attrs, reason))
        offset += length
    return records


class UsnJournal:
    """Open an existing NTFS journal read-only; never create/reset a journal."""

    def __init__(self, root):
        if not IS_WINDOWS:
            raise NativeUnavailable('USN journals are Windows-only.')
        drive, _ = os.path.splitdrive(os.path.abspath(root))
        if len(drive) != 2 or drive[1] != ':':
            raise NativeUnavailable('USN journals require a local volume.')
        self.handle = kernel.CreateFileW('\\\\.\\' + drive, 0, 7, None, 3, 0, None)
        self.control = 0x000903AB  # FSCTL_READ_UNPRIVILEGED_USN_JOURNAL
        try:
            self._probe_read(self.query())
        except OSError:
            self.close()
            self.handle = kernel.CreateFileW('\\\\.\\' + drive, 0x80000000, 7,
                                             None, 3, 0, None)
            self.control = 0x000900BB  # FSCTL_READ_USN_JOURNAL
            try:
                self._probe_read(self.query())
            except OSError:
                self.close()
                raise NativeUnavailable('The NTFS journal cannot be read with current permissions.')

    def _ioctl(self, code, data=b'', output_size=1024 * 1024):
        if self.handle in (None, INVALID_HANDLE):
            raise NativeUnavailable('The NTFS volume cannot be opened.')
        output = ctypes.create_string_buffer(output_size)
        source = ctypes.create_string_buffer(data) if data else None
        returned = wintypes.DWORD()
        if not kernel.DeviceIoControl(self.handle, code, source, len(data), output,
                                      len(output), ctypes.byref(returned), None):
            raise ctypes.WinError(ctypes.get_last_error())
        return output.raw[:returned.value]

    def query(self):
        data = self._ioctl(0x000900F4, output_size=128)  # FSCTL_QUERY_USN_JOURNAL
        if len(data) < 56:
            raise NativeUnavailable('Invalid NTFS journal information.')
        journal_id, first, next_usn, lowest = struct.unpack_from('<Qqqq', data)
        return {'id': str(journal_id), 'first': max(first, lowest), 'next': next_usn}

    def _probe_read(self, current):
        data = struct.pack('<qIIQQQHH4x', current['next'], 0xFFFFFFFF, 0, 0, 0,
                           int(current['id']), 2, 3)
        response = self._ioctl(self.control, data, output_size=4096)
        if len(response) < 8:
            raise NativeUnavailable('The existing NTFS journal cannot be read.')

    def changes(self, cursor, journal_id, cancel=None):
        current = self.query()
        if str(journal_id) != current['id'] or not current['first'] <= cursor <= current['next']:
            raise NativeUnavailable('The saved NTFS journal cursor has expired.')
        records = []
        while cursor < current['next']:
            if cancel and cancel.is_set():
                raise InterruptedError('Index update cancelled.')
            data = struct.pack('<qIIQQQHH4x', cursor, 0xFFFFFFFF, 0, 0, 0,
                               int(current['id']), 2, 3)
            output = self._ioctl(self.control, data)
            if len(output) < 8:
                raise NativeUnavailable('Invalid NTFS journal response.')
            next_cursor = struct.unpack_from('<q', output)[0]
            if next_cursor <= cursor:
                raise NativeUnavailable('The NTFS journal did not advance.')
            records.extend(parse_usn_records(output))
            cursor = next_cursor
        return records, {'id': current['id'], 'next': cursor}

    def close(self):
        if getattr(self, 'handle', None) not in (None, INVALID_HANDLE):
            kernel.CloseHandle(self.handle)
        self.handle = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


if IS_WINDOWS:
    class OVERLAPPED(ctypes.Structure):
        _fields_ = [('Internal', ctypes.c_size_t), ('InternalHigh', ctypes.c_size_t),
                    ('Offset', wintypes.DWORD), ('OffsetHigh', wintypes.DWORD),
                    ('hEvent', wintypes.HANDLE)]

    kernel.CreateEventW.argtypes = (ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL,
                                   wintypes.LPCWSTR)
    kernel.CreateEventW.restype = wintypes.HANDLE
    kernel.ResetEvent.argtypes = (wintypes.HANDLE,)
    kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.ReadDirectoryChangesW.argtypes = (
        wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, wintypes.BOOL, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(OVERLAPPED), ctypes.c_void_p)
    kernel.ReadDirectoryChangesW.restype = wintypes.BOOL
    kernel.GetOverlappedResult.argtypes = (
        wintypes.HANDLE, ctypes.POINTER(OVERLAPPED), ctypes.POINTER(wintypes.DWORD), wintypes.BOOL)
    kernel.GetOverlappedResult.restype = wintypes.BOOL
    kernel.CancelIoEx.argtypes = (wintypes.HANDLE, ctypes.POINTER(OVERLAPPED))
    kernel.CancelIoEx.restype = wintypes.BOOL


def parse_notifications(data, root):
    result = []
    offset = 0
    while offset < len(data):
        if offset + 12 > len(data):
            raise OSError('Truncated directory notification.')
        next_offset, action, length = struct.unpack_from('<III', data, offset)
        if action not in (1, 2, 3, 4, 5) or not length or length % 2 or offset + 12 + length > len(data):
            raise OSError('Invalid directory notification filename.')
        name = data[offset + 12:offset + 12 + length].decode('utf-16-le')
        result.append((action, os.path.join(root, name)))
        if not next_offset:
            break
        if next_offset < 12 or offset + next_offset >= len(data):
            raise OSError('Invalid directory notification offset.')
        offset += next_offset
    return result


class DirectoryWatcher:
    """Capture subtree changes without polling every file or touching Tk."""

    def __init__(self, root, callback):
        self.root, self.callback = root, callback
        self._stop = threading.Event()
        self._ready = threading.Event()
        self.thread = None
        self.error = None
        self.handle = None

    def start(self):
        if self.running:
            return True
        if self._stop.is_set():
            return False
        if not IS_WINDOWS:
            return False
        self.thread = threading.Thread(target=self._run, name='directory-notifications', daemon=True)
        self.thread.start()
        self._ready.wait(3)
        return self._ready.is_set() and self.error is None

    def _run(self):
        event = None
        try:
            self.handle = _open_directory(self.root, overlapped=True)
            event = kernel.CreateEventW(None, True, False, None)
            if not event:
                raise ctypes.WinError(ctypes.get_last_error())
            buffer = ctypes.create_string_buffer(64 * 1024)
            while not self._stop.is_set():
                kernel.ResetEvent(event)
                overlap = OVERLAPPED(hEvent=event)
                ok = kernel.ReadDirectoryChangesW(self.handle, buffer, len(buffer), True,
                                                   0x5F, None, ctypes.byref(overlap), None)
                if not ok and ctypes.get_last_error() != 997:
                    raise ctypes.WinError(ctypes.get_last_error())
                self._ready.set()
                while kernel.WaitForSingleObject(event, 300) == 258:
                    if self._stop.is_set():
                        kernel.CancelIoEx(self.handle, ctypes.byref(overlap))
                        break
                returned = wintypes.DWORD()
                ok = kernel.GetOverlappedResult(self.handle, ctypes.byref(overlap),
                                                ctypes.byref(returned), True)
                if self._stop.is_set():
                    break
                if not ok:
                    raise ctypes.WinError(ctypes.get_last_error())
                if not returned.value:
                    self.callback([], True)  # buffer overflow: reconcile fingerprints
                else:
                    self.callback(parse_notifications(buffer.raw[:returned.value], self.root), False)
        except OSError as error:
            self.error = error
            self._ready.set()
            if not self._stop.is_set():
                self.callback([], True)
        finally:
            if event:
                kernel.CloseHandle(event)
            if self.handle not in (None, INVALID_HANDLE):
                kernel.CloseHandle(self.handle)
            self.handle = None

    @property
    def running(self):
        return bool(self.thread and self.thread.is_alive() and self.error is None)

    def close(self):
        self._stop.set()
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(timeout=2)
