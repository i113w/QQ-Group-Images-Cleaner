"""Metadata shared by the native enumerator, index and UI."""

from dataclasses import dataclass
import os
import time


IMAGE_EXTENSIONS = frozenset({'.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp',
                              '.tif', '.tiff', '.avif', '.heic'})


def path_key(path):
    return os.path.normcase(os.path.abspath(os.path.normpath(os.fspath(path))))


def is_within(path, root):
    try:
        return os.path.commonpath((path_key(path), path_key(root))) == path_key(root)
    except ValueError:
        return False


@dataclass(frozen=True, slots=True)
class FileRecord:
    path: str
    size: int
    ctime_ns: int
    mtime_ns: int
    atime_ns: int

    @property
    def real_time(self):
        valid = [stamp for stamp in (self.ctime_ns, self.mtime_ns, self.atime_ns)
                 if stamp > 0]
        return min(valid, default=0) / 1_000_000_000

    def row(self, generation):
        stamp = self.real_time
        try:
            local = time.localtime(stamp)
            year, month = local.tm_year, local.tm_mon
        except (OverflowError, OSError, ValueError):
            year, month = 1970, 1
        return (path_key(self.path), self.path, path_key(os.path.dirname(self.path)),
                os.path.basename(self.path), self.size, self.ctime_ns, self.mtime_ns,
                self.atime_ns, stamp, year, month,
                os.path.splitext(self.path)[1].lower(), 1, generation)


@dataclass(frozen=True, slots=True)
class DirectoryRecord:
    path: str
    mtime_ns: int
    ctime_ns: int
    file_id: str = ''

    def row(self, generation):
        return (path_key(self.path), self.path, path_key(os.path.dirname(self.path)),
                self.mtime_ns, self.ctime_ns, self.file_id, 1, generation)


@dataclass(slots=True)
class DirectoryListing:
    directory: DirectoryRecord
    files: list
    children: list


def format_size(size):
    value = float(size)
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if value < 1024 or unit == 'TB':
            return f'{value:,.0f} {unit}' if unit == 'B' else f'{value:,.2f} {unit}'
        value /= 1024
