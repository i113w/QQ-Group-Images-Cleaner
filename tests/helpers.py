import os
from pathlib import Path
import uuid


def fixture(name):
    base = Path(os.environ.get('QQ_CLEANER_TEST_WORK',
                str(Path(__file__).resolve().parents[1] / '.qq-cleaner' / 'test-data'))).absolute()
    directory = base / (name + '-' + uuid.uuid4().hex)
    directory.mkdir(parents=True, exist_ok=False)
    root = directory / 'Group2'
    root.mkdir()
    outside = directory / 'kept-outside-index'
    outside.mkdir()
    return directory, root, outside


def write_file(path, content=b'data'):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('xb') as handle:
        handle.write(content)
    return path


def move_fixture(source, target, scope):
    """Resolve and check both endpoints before any file/directory rename."""
    checked_scope = scope.resolve(strict=True)
    checked_source = source.resolve(strict=True)
    checked_target = target.parent.resolve(strict=True) / target.name
    if not checked_source.is_relative_to(checked_scope) or not checked_target.is_relative_to(checked_scope):
        raise AssertionError('A fixture move would escape the explicitly created workspace.')
    if checked_target.exists():
        raise FileExistsError(checked_target)
    checked_source.rename(checked_target)


def force_directory_change(path, old_ns):
    metadata = path.stat()
    os.utime(path, ns=(metadata.st_atime_ns, old_ns + 2_000_000_000))
