"""Explicit file plans and Windows recycling with a fail-closed progress sink.

There is intentionally no permanent-delete implementation or fallback.
The GUI approves a particular immutable plan, not an arbitrary later path list.
"""

import ctypes
from ctypes import wintypes
from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import time
import uuid

from lib.file_metadata import is_within, path_key


class RecycleSafetyError(OSError):
    pass


class NoRecycleTargets(RecycleSafetyError):
    pass


@dataclass(frozen=True, slots=True)
class RecycleTarget:
    path: str
    size: int
    mtime_ns: int
    ctime_ns: int
    device: int
    inode: int
    real_time: float


@dataclass(frozen=True, slots=True)
class RecyclePlan:
    id: str
    root: str
    reason: str
    targets: tuple
    created_at: float
    cutoff: tuple | None = None

    @property
    def total_size(self):
        return sum(target.size for target in self.targets)

    def manifest(self):
        return {'id': self.id, 'root': self.root, 'reason': self.reason,
                'created_at': self.created_at, 'file_count': len(self.targets),
                'total_size': self.total_size, 'destination': 'Windows Recycle Bin',
                'cutoff': self.cutoff,
                'targets': [asdict(target) for target in self.targets]}

    @property
    def digest(self):
        text = json.dumps(self.manifest(), ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(text.encode('utf-8')).hexdigest()


@dataclass(frozen=True, slots=True)
class RecycleApproval:
    plan_id: str
    digest: str


@dataclass
class RecycleResult:
    recycled: list
    stopped_reason: str = ''


def _snapshot(root, path):
    if not os.path.isabs(path):
        raise RecycleSafetyError('An absolute file path is required: ' + os.fspath(path))
    absolute = os.path.abspath(os.path.normpath(path))
    if path_key(absolute) == path_key(root) or not is_within(absolute, root):
        raise RecycleSafetyError('The file is outside the selected directory: ' + absolute)
    # Check each ancestor. A junction/symlink must not redirect a literal path
    # outside the user's approved folder, even if it was added after indexing.
    current = absolute
    while path_key(current) != path_key(root):
        metadata = os.stat(current, follow_symlinks=False)
        if stat.S_ISLNK(metadata.st_mode) or getattr(metadata, 'st_file_attributes', 0) & 0x400:
            raise RecycleSafetyError('Symbolic links and reparse points are excluded: ' + current)
        current = os.path.dirname(current)
    metadata = os.stat(absolute, follow_symlinks=False)
    if not stat.S_ISREG(metadata.st_mode):
        raise RecycleSafetyError('Only explicitly listed regular files can be recycled: ' + absolute)
    if path_key(os.path.realpath(absolute)) != path_key(absolute):
        raise RecycleSafetyError('The resolved file path differs from the approved path: ' + absolute)
    created = getattr(metadata, 'st_birthtime_ns', metadata.st_ctime_ns)
    valid_times = [stamp for stamp in (created, metadata.st_mtime_ns, metadata.st_atime_ns) if stamp > 0]
    real_time = min(valid_times, default=0) / 1_000_000_000
    return RecycleTarget(absolute, metadata.st_size, metadata.st_mtime_ns, created,
                         metadata.st_dev, metadata.st_ino, real_time)


def make_plan(root, paths, reason, *, cutoff=None):
    root = str(Path(root).resolve(strict=True))
    if not os.path.isdir(root) or not reason.strip():
        raise RecycleSafetyError('A valid directory and a specific cleanup reason are required.')
    targets, seen = [], set()
    for path in paths:
        key = path_key(path)
        if key not in seen:
            target = _snapshot(root, os.fspath(path))
            if cutoff is not None:
                current_date = time.localtime(target.real_time)
                if (current_date.tm_year, current_date.tm_mon) > tuple(cutoff):
                    seen.add(key)
                    continue
            targets.append(target)
            seen.add(key)
    if not targets:
        raise NoRecycleTargets('No current files match the cleanup plan.')
    targets.sort(key=lambda target: path_key(target.path))
    return RecyclePlan(str(uuid.uuid4()), root, reason, tuple(targets), time.time(), cutoff)


def validate_approval(plan, approval):
    if (not isinstance(approval, RecycleApproval) or approval.plan_id != plan.id or
        approval.digest != plan.digest):
        raise RecycleSafetyError('Explicit approval for this exact file plan is required.')
    for target in plan.targets:
        if _snapshot(plan.root, target.path) != target:
            raise RecycleSafetyError('A file changed after preview; create and confirm a new plan: ' + target.path)


def save_manifest(plan, directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (plan.id + '.json')
    # Keep every plan for inspection; never overwrite or remove an earlier plan.
    with path.open('x', encoding='utf-8') as handle:
        json.dump({**plan.manifest(), 'digest': plan.digest}, handle, ensure_ascii=False, indent=2)
    return path


if sys.platform == 'win32':
    import winreg

    HRESULT = ctypes.c_int32
    S_OK, E_ABORT, E_NOINTERFACE = 0, -2147467260, -2147467262
    TSF_DELETE_RECYCLE_IF_POSSIBLE = 0x80
    # The shell must stop on errors, request recycling, and warn on any nuke path.
    OPERATION_FLAGS = 0x00080000 | 0x00100000 | 0x20000000 | 0x0400 | 0x4000 | 0x0004
    ole = ctypes.OleDLL('ole32')
    shell = ctypes.OleDLL('shell32')
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)

    class GUID(ctypes.Structure):
        _fields_ = [('Data1', wintypes.DWORD), ('Data2', wintypes.WORD), ('Data3', wintypes.WORD),
                    ('Data4', ctypes.c_ubyte * 8)]

        @classmethod
        def from_text(cls, text):
            return cls.from_buffer_copy(uuid.UUID(text).bytes_le)

    class SHQUERYRBINFO(ctypes.Structure):
        _fields_ = [('cbSize', wintypes.DWORD), ('i64Size', ctypes.c_int64),
                    ('i64NumItems', ctypes.c_int64)]

    class SinkObject(ctypes.Structure):
        _fields_ = [('vtable', ctypes.POINTER(ctypes.c_void_p))]

    ole.CoInitializeEx.argtypes = (ctypes.c_void_p, wintypes.DWORD)
    ole.CoInitializeEx.restype = HRESULT
    ole.CoUninitialize.argtypes = ()
    ole.CoCreateInstance.argtypes = (ctypes.POINTER(GUID), ctypes.c_void_p, wintypes.DWORD,
                                     ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p))
    ole.CoCreateInstance.restype = HRESULT
    ole.CoTaskMemFree.argtypes = (ctypes.c_void_p,)
    shell.SHCreateItemFromParsingName.argtypes = (
        wintypes.LPCWSTR, ctypes.c_void_p, ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p))
    shell.SHCreateItemFromParsingName.restype = HRESULT
    shell.SHQueryRecycleBinW.argtypes = (wintypes.LPCWSTR, ctypes.POINTER(SHQUERYRBINFO))
    shell.SHQueryRecycleBinW.restype = HRESULT
    kernel.GetVolumePathNameW.argtypes = (wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD)
    kernel.GetVolumePathNameW.restype = wintypes.BOOL
    kernel.GetVolumeNameForVolumeMountPointW.argtypes = (
        wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD)
    kernel.GetVolumeNameForVolumeMountPointW.restype = wintypes.BOOL

    def _call(interface, index, argtypes=(), *args, result=HRESULT):
        vtable = ctypes.cast(interface, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        function = ctypes.WINFUNCTYPE(result, ctypes.c_void_p, *argtypes)(vtable[index])
        return function(interface, *args)

    def _check(hr, operation):
        if hr < 0:
            raise RecycleSafetyError(f'{operation}: HRESULT 0x{hr & 0xFFFFFFFF:08X}')

    def _shell_path(item):
        pointer = ctypes.c_void_p()
        _check(_call(item, 5, (wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p)),
                     0x80058000, ctypes.byref(pointer)), 'Get approved shell file path')
        try:
            return ctypes.wstring_at(pointer)
        finally:
            ole.CoTaskMemFree(pointer)

    def recycle_capacity(root):
        global_nuke = False
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                r'Software\Microsoft\Windows\CurrentVersion\Explorer\BitBucket') as key:
                global_nuke = bool(winreg.QueryValueEx(key, 'NukeOnDelete')[0])
        except OSError:
            pass
        if global_nuke:
            raise RecycleSafetyError('Windows is configured for permanent deletion. Operation stopped.')
        volume_path, volume_name = ctypes.create_unicode_buffer(32768), ctypes.create_unicode_buffer(128)
        if not kernel.GetVolumePathNameW(root, volume_path, len(volume_path)):
            raise RecycleSafetyError('Cannot identify the selected file volume.')
        if not kernel.GetVolumeNameForVolumeMountPointW(volume_path.value, volume_name, len(volume_name)):
            raise RecycleSafetyError('This volume does not expose a verifiable local Recycle Bin.')
        identity = volume_name.value[volume_name.value.find('{'):volume_name.value.find('}') + 1]
        registry_path = r'Software\Microsoft\Windows\CurrentVersion\Explorer\BitBucket\Volume' + chr(92) + identity
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, registry_path) as key:
                capacity_mb = winreg.QueryValueEx(key, 'MaxCapacity')[0]
                disabled = winreg.QueryValueEx(key, 'NukeOnDelete')[0]
        except OSError as error:
            raise RecycleSafetyError('Recycle Bin capacity cannot be verified. Set a custom capacity in '
                                     'Windows Recycle Bin properties before retrying.') from error
        if disabled or not isinstance(capacity_mb, int) or capacity_mb <= 0:
            raise RecycleSafetyError('The Recycle Bin is disabled or has no usable capacity for this volume.')
        info = SHQUERYRBINFO(cbSize=ctypes.sizeof(SHQUERYRBINFO))
        _check(shell.SHQueryRecycleBinW(volume_path.value, ctypes.byref(info)), 'Query Recycle Bin usage')
        return max(0, capacity_mb * 1024 * 1024 - info.i64Size)

    def guard_capacity(plan):
        # Reserve metadata space as well as file sizes so recycling cannot evict
        # existing bin contents because the configured capacity is exhausted.
        required = plan.total_size + len(plan.targets) * 4096 + 4 * 1024 * 1024
        if required > recycle_capacity(plan.root):
            raise RecycleSafetyError('The files exceed available Recycle Bin capacity. Operation stopped; '
                                     'permanent deletion is unavailable.')

    class RecycleProgressSink:
        def __init__(self, plan, progress=None):
            self.plan, self.progress = plan, progress
            self.targets = {path_key(target.path): target for target in plan.targets}
            self.recycled, self.stopped_reason = [], ''
            self.references = 1
            self.callbacks = []

            def query(_, iid, output):
                supported = (uuid.UUID('00000000-0000-0000-C000-000000000046').bytes_le,
                             uuid.UUID('04B0F1A7-9490-44BC-96E1-4296A31252E2').bytes_le)
                if ctypes.string_at(iid, 16) in supported:
                    output[0] = self.pointer
                    self.references += 1
                    return S_OK
                output[0] = None
                return E_NOINTERFACE

            def add_ref(_):
                self.references += 1
                return self.references

            def release(_):
                self.references = max(0, self.references - 1)
                return self.references

            def pre_delete(_, flags, item):
                try:
                    if self.stopped_reason:
                        return E_ABORT
                    if not flags & TSF_DELETE_RECYCLE_IF_POSSIBLE:
                        raise RecycleSafetyError('Windows requested a permanent-delete path. Operation stopped.')
                    path = _shell_path(item)
                    target = self.targets.get(path_key(path))
                    if target is None or _snapshot(plan.root, path) != target:
                        raise RecycleSafetyError('The shell target differs from the approved file snapshot.')
                    return S_OK
                except BaseException as error:
                    self.stopped_reason = str(error)
                    return E_ABORT

            def post_delete(_, flags, item, hr, new_item):
                try:
                    if hr < 0:
                        raise RecycleSafetyError(f'Recycling failed: HRESULT 0x{hr & 0xFFFFFFFF:08X}')
                    if not new_item:
                        raise RecycleSafetyError('Windows did not report a recycled destination. Operation stopped.')
                    path = _shell_path(item)
                    if path_key(path) not in self.targets:
                        raise RecycleSafetyError('Unexpected shell target after recycling.')
                    if path not in self.recycled:
                        self.recycled.append(path)
                    if self.progress:
                        self.progress(len(self.recycled), len(plan.targets))
                    return S_OK
                except BaseException as error:
                    self.stopped_reason = str(error)
                    return E_ABORT

            def finish(_, hr):
                if hr < 0 and not self.stopped_reason:
                    self.stopped_reason = f'Windows stopped recycling: HRESULT 0x{hr & 0xFFFFFFFF:08X}'
                return S_OK

            def callback(restype, types, handler):
                function = ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *types)(handler)
                self.callbacks.append(function)
                return ctypes.cast(function, ctypes.c_void_p).value

            ptr, dword, text = ctypes.c_void_p, wintypes.DWORD, wintypes.LPCWSTR
            functions = [
                callback(HRESULT, (ptr, ctypes.POINTER(ptr)), query),
                callback(wintypes.ULONG, (), add_ref), callback(wintypes.ULONG, (), release),
                callback(HRESULT, (), lambda *_: S_OK),
                callback(HRESULT, (HRESULT,), finish),
                callback(HRESULT, (dword, ptr, text), lambda *_: E_ABORT),
                callback(HRESULT, (dword, ptr, text, HRESULT, ptr), lambda *_: E_ABORT),
                callback(HRESULT, (dword, ptr, ptr, text), lambda *_: E_ABORT),
                callback(HRESULT, (dword, ptr, ptr, text, HRESULT, ptr), lambda *_: E_ABORT),
                callback(HRESULT, (dword, ptr, ptr, text), lambda *_: E_ABORT),
                callback(HRESULT, (dword, ptr, ptr, text, HRESULT, ptr), lambda *_: E_ABORT),
                callback(HRESULT, (dword, ptr), pre_delete),
                callback(HRESULT, (dword, ptr, HRESULT, ptr), post_delete),
                callback(HRESULT, (dword, ptr, text), lambda *_: E_ABORT),
                callback(HRESULT, (dword, ptr, text, text, dword, HRESULT, ptr), lambda *_: E_ABORT),
                callback(HRESULT, (dword, dword), lambda *_: S_OK),
                callback(HRESULT, (), lambda *_: S_OK), callback(HRESULT, (), lambda *_: S_OK),
                callback(HRESULT, (), lambda *_: S_OK),
            ]
            self.vtable = (ctypes.c_void_p * len(functions))(*functions)
            self.object = SinkObject(self.vtable)
            self.pointer = ctypes.cast(ctypes.pointer(self.object), ctypes.c_void_p)


def recycle(plan, approval, progress=None, owner_window=None):
    validate_approval(plan, approval)
    if sys.platform != 'win32':
        raise RecycleSafetyError('Verified recycling is supported only on Windows. No file was changed.')
    guard_capacity(plan)
    operation, items, cookie = ctypes.c_void_p(), [], wintypes.DWORD()
    initialized = False
    sink = RecycleProgressSink(plan, progress)
    try:
        _check(ole.CoInitializeEx(None, 2), 'Initialize recycling apartment')
        initialized = True
        _check(ole.CoCreateInstance(
            ctypes.byref(GUID.from_text('3AD05575-8857-4850-9277-11B85BDB8E09')), None, 1,
            ctypes.byref(GUID.from_text('947AAB5F-0A5C-4C13-B4D6-4BF7836FC9F8')),
            ctypes.byref(operation)), 'Create Windows recycle operation')
        _check(_call(operation, 5, (wintypes.DWORD,), OPERATION_FLAGS), 'Set recycle-only flags')
        if owner_window:
            _check(_call(operation, 9, (wintypes.HWND,), owner_window), 'Set recycle owner window')
        _check(_call(operation, 3, (ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD)),
                     sink.pointer, ctypes.byref(cookie)), 'Install recycle safety guard')
        for target in plan.targets:
            item = ctypes.c_void_p()
            _check(shell.SHCreateItemFromParsingName(target.path, None,
                ctypes.byref(GUID.from_text('43826D1E-E718-42EE-BC55-A1E261C37BFE')),
                ctypes.byref(item)), 'Bind exact approved file')
            items.append(item)
            _check(_call(operation, 18, (ctypes.c_void_p, ctypes.c_void_p), item, None),
                   'Queue approved file for recycling')
        hr = _call(operation, 21)
        aborted = wintypes.BOOL()
        _call(operation, 22, (ctypes.POINTER(wintypes.BOOL),), ctypes.byref(aborted))
        if (hr < 0 or aborted.value) and not sink.stopped_reason:
            sink.stopped_reason = f'Windows stopped recycling (0x{hr & 0xFFFFFFFF:08X}).'
        return RecycleResult(sink.recycled, sink.stopped_reason)
    finally:
        if operation:
            if cookie.value:
                _call(operation, 4, (wintypes.DWORD,), cookie.value)
            _call(operation, 2, result=wintypes.ULONG)
        for item in items:
            _call(item, 2, result=wintypes.ULONG)
        if initialized:
            ole.CoUninitialize()
