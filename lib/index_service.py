"""Cached startup, native directory batches and targeted incremental updates."""

from collections import deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import ExitStack
from dataclasses import dataclass, field
import os
import json
from pathlib import Path
import queue
import stat
import threading
import time

from lib.file_metadata import DirectoryListing, DirectoryRecord, FileRecord, is_within, path_key
from lib.index_store import DIRECTORY_UPSERT, FILE_UPSERT, IndexStore
from lib.windows_native import (DirectoryWatcher, IS_WINDOWS, NativeUnavailable,
                                UsnJournal, enumerate_directory)


@dataclass
class UpdateResult:
    mode: str
    changed: int
    files: int
    directories: int
    elapsed: float
    backend: str
    errors: list = field(default_factory=list)


class DirectoryScanner:
    def __init__(self, excluded=(), workers=8):
        self.excluded = tuple(map(path_key, excluded))
        self.workers = max(1, min(workers, 16))
        self.backend = 'Windows bulk metadata' if IS_WINDOWS else 'os.scandir'

    def allowed(self, path):
        if not self.excluded:
            return True
        key = path_key(path)
        return not any(key == excluded or key.startswith(excluded + os.sep) for excluded in self.excluded)

    @staticmethod
    def portable_listing(path):
        before = os.stat(path, follow_symlinks=False)
        files, children = [], []
        with os.scandir(path) as entries:
            for entry in entries:
                if entry.is_symlink():
                    continue
                metadata = entry.stat(follow_symlinks=False)
                if getattr(metadata, 'st_file_attributes', 0) & 0x400:
                    continue
                created = getattr(metadata, 'st_birthtime_ns', metadata.st_ctime_ns)
                if entry.is_dir(follow_symlinks=False):
                    children.append(DirectoryRecord(entry.path, metadata.st_mtime_ns, created))
                elif entry.is_file(follow_symlinks=False):
                    files.append(FileRecord(entry.path, metadata.st_size, created,
                                            metadata.st_mtime_ns, metadata.st_atime_ns))
        return DirectoryListing(DirectoryRecord(path, before.st_mtime_ns,
            getattr(before, 'st_birthtime_ns', before.st_ctime_ns)), files, children)

    def listing(self, path):
        if IS_WINDOWS and self.backend == 'Windows bulk metadata':
            try:
                listing = enumerate_directory(path)
            except NativeUnavailable:
                self.backend = 'os.scandir'
                listing = self.portable_listing(path)
        else:
            listing = self.portable_listing(path)
        if self.excluded:
            listing.children = [record for record in listing.children if self.allowed(record.path)]
        return listing

    def walk(self, root, cancel=None):
        """Bound the queued work; memory does not grow with all directory paths."""
        pending = deque([root])
        with ThreadPoolExecutor(max_workers=self.workers, thread_name_prefix='directory-batches') as executor:
            running = {}
            while pending or running:
                if cancel and cancel.is_set():
                    raise InterruptedError('Index update cancelled.')
                while pending and len(running) < self.workers * 2:
                    path = pending.popleft()
                    running[executor.submit(self.listing, path)] = path
                completed, _ = wait(running, return_when=FIRST_COMPLETED)
                for future in completed:
                    path = running.pop(future)
                    try:
                        listing = future.result()
                    except OSError as error:
                        yield None, {'path': path, 'message': str(error)}
                        continue
                    pending.extend(record.path for record in listing.children)
                    yield listing, None


class IndexService:
    def __init__(self, root, cache_dir):
        self.root = str(Path(root).resolve(strict=True))
        if not os.path.isdir(self.root):
            raise ValueError('Select a directory to index.')
        cache_path = Path(cache_dir).absolute()
        excluded = cache_path.parent if cache_path.parent.name == '.qq-cleaner' else cache_path
        if is_within(self.root, excluded):
            raise ValueError('Choose an image folder outside the application index-cache directory.')
        self.store = IndexStore(self.root, cache_dir)
        self.scanner = DirectoryScanner(excluded=(excluded,) if is_within(excluded, self.root) else ())
        self.events = queue.Queue()
        self.watcher = DirectoryWatcher(self.root, self._receive_changes)
        self.lock = threading.RLock()
        self.closed = False

    def _receive_changes(self, events, overflow):
        if not self.closed:
            filtered = [(action, path) for action, path in events
                        if is_within(path, self.root) and self.scanner.allowed(path)]
            if filtered or overflow:
                self.events.put((filtered, overflow))

    def start_watching(self):
        return self.watcher.start()

    def drain_events(self):
        paths, overflow = set(), False
        while True:
            try:
                events, lost = self.events.get_nowait()
            except queue.Empty:
                break
            overflow |= lost
            paths.update(path for _, path in events if is_within(path, self.root)
                         and self.scanner.allowed(path))
        return paths, overflow

    @property
    def pending(self):
        return not self.events.empty()

    def _journal(self):
        journal = None
        try:
            journal = UsnJournal(self.root)
            return journal, journal.query()
        except OSError:
            if journal:
                journal.close()
            return None, None

    def rebuild(self, progress=None, cancel=None):
        started = time.perf_counter()
        errors, file_count, directory_count = [], 0, 0
        journal, cursor = self._journal()
        try:
            with self.lock, self.store.connect() as connection:
                connection.execute('BEGIN IMMEDIATE')
                generation = int(self.store._meta(connection, 'generation', '0')) + 1
                last_progress = 0.0
                for listing, error in self.scanner.walk(self.root, cancel):
                    if error:
                        errors.append(error)
                        continue
                    connection.executemany(FILE_UPSERT, (self.store.file_row(record, generation) for record in listing.files))
                    connection.execute(DIRECTORY_UPSERT, self.store.directory_row(listing.directory, generation))
                    file_count += len(listing.files)
                    directory_count += 1
                    now = time.perf_counter()
                    if progress and now - last_progress >= 0.15:
                        progress(file_count, directory_count)
                        last_progress = now
                if cancel and cancel.is_set():
                    raise InterruptedError('Index update cancelled.')
                self.store.finalize_full(connection, generation, errors)
                self.store.ensure_indexes(connection)
                if cancel and cancel.is_set():
                    raise InterruptedError('Index update cancelled.')
                self.store.set_meta(connection, 'generation', generation)
                self.store.finish_update(connection, 'full', errors, cursor)
        finally:
            if journal:
                journal.close()
        summary = self.store.summary()
        return UpdateResult('full', file_count, summary['file_count'], directory_count,
                            time.perf_counter() - started, self.scanner.backend, errors)

    def _fingerprint_changes(self, known, cancel=None):
        dirty = set()
        for key, record in known.items():
            if cancel and cancel.is_set():
                raise InterruptedError('Index update cancelled.')
            try:
                metadata = os.stat(record['display_path'], follow_symlinks=False)
                created = getattr(metadata, 'st_birthtime_ns', metadata.st_ctime_ns)
                if (not stat.S_ISDIR(metadata.st_mode) or
                    getattr(metadata, 'st_file_attributes', 0) & 0x400 or
                    metadata.st_mtime_ns != record['mtime_ns'] or created != record['ctime_ns']):
                    dirty.add(record['display_path'])
            except FileNotFoundError:
                dirty.add(record['display_path'])
            except OSError:
                # Enumeration records the precise error and preserves prior data.
                dirty.add(record['display_path'])
        return dirty

    def _journal_paths(self, journal, info, known, cancel):
        if not info.get('journal_cursor') or not info.get('journal_id'):
            raise NativeUnavailable('No saved journal cursor.')
        records, cursor = journal.changes(int(info['journal_cursor']), info['journal_id'], cancel)
        ids = {row['file_id']: row['display_path'] for row in known.values() if row['file_id']}
        paths = set()
        for file_id, parent_id, name, attrs, _ in records:
            parent = ids.get(parent_id)
            if parent:
                paths.add(os.path.join(parent, name) if name else parent)
            elif attrs & 0x10 and file_id in ids:
                paths.add(ids[file_id])
        return paths, cursor

    def refresh(self, *, force=False, progress=None, cancel=None, events_only=False):
        if force or not self.store.ready:
            return self.rebuild(progress, cancel)
        started = time.perf_counter()
        paths, overflow = self.drain_events()
        if overflow:
            # Notifications can lose content-only changes. A full verification
            # is required here; it is never the routine refresh path.
            return self.rebuild(progress, cancel)
        errors, changed, examined = [], 0, 0
        journal, cursor = self._journal() if not events_only else (None, None)
        with ExitStack() as cleanup, self.lock, self.store.connect() as connection:
            if journal:
                cleanup.callback(journal.close)
            connection.execute('BEGIN IMMEDIATE')
            known = self.store.directories(connection)
            info = {row['key']: row['value'] for row in connection.execute('SELECT * FROM meta')}
            mode = 'notifications' if events_only else 'directory'
            if not events_only:
                if journal:
                    try:
                        missed, cursor = self._journal_paths(journal, info, known, cancel)
                        paths.update(missed)
                        mode = 'usn'
                    except OSError:
                        # An expired/malformed journal can lose content-only
                        # changes. Establish a new baseline once, atomically.
                        connection.rollback()
                        return self.rebuild(progress, cancel)
                else:
                    paths.update(self._fingerprint_changes(known, cancel))
            generation = int(info.get('generation', '0')) + 1
            paths.update(error['path'] for error in json.loads(info.get('errors', '[]')))
            queue_paths = deque(sorted(paths, key=lambda path: len(path_key(path))))
            processed = set()
            parents = set()
            while queue_paths:
                if cancel and cancel.is_set():
                    raise InterruptedError('Index update cancelled.')
                path = queue_paths.popleft()
                if (not is_within(path, self.root) or not self.scanner.allowed(path) or
                    path_key(path) in processed):
                    continue
                key = path_key(path)
                processed.add(key)
                parents.add(os.path.dirname(path))
                try:
                    metadata = os.stat(path, follow_symlinks=False)
                    reparse = bool(getattr(metadata, 'st_file_attributes', 0) & 0x400) or stat.S_ISLNK(metadata.st_mode)
                    if reparse:
                        changed += self.store.deactivate_paths(connection, [path])
                        if key in known:
                            changed += self.store.deactivate_subtree(connection, path)
                    elif stat.S_ISDIR(metadata.st_mode):
                        listing = self.scanner.listing(path)
                        changed += self.store.reconcile_listing(connection, listing, generation)
                        examined += 1
                        for child in listing.children:
                            old = known.get(path_key(child.path))
                            if (old is None or child.mtime_ns != old['mtime_ns'] or
                                child.ctime_ns != old['ctime_ns'] or
                                child.file_id != old['file_id']):
                                queue_paths.append(child.path)
                        if progress:
                            progress(changed, examined)
                    elif stat.S_ISREG(metadata.st_mode):
                        if key in known:
                            changed += self.store.deactivate_subtree(connection, path)
                        created = getattr(metadata, 'st_birthtime_ns', metadata.st_ctime_ns)
                        changed += self.store.upsert_files(connection, [FileRecord(path, metadata.st_size,
                            created, metadata.st_mtime_ns, metadata.st_atime_ns)], generation)
                except FileNotFoundError:
                    changed += self.store.deactivate_paths(connection, [path])
                    if key in known:
                        changed += self.store.deactivate_subtree(connection, path)
                except OSError as error:
                    errors.append({'path': path, 'message': str(error)})
            # Keep parent fingerprints current after exact file notifications.
            for parent in parents:
                key = path_key(parent)
                if key not in known:
                    continue
                try:
                    metadata = os.stat(parent, follow_symlinks=False)
                    connection.execute('UPDATE directories SET mtime_ns=?,ctime_ns=? WHERE path=?',
                        (metadata.st_mtime_ns, getattr(metadata, 'st_birthtime_ns', metadata.st_ctime_ns), self.store._key(parent)))
                except OSError:
                    pass
            self.store.set_meta(connection, 'generation', generation)
            # Online notification updates keep the last offline journal cursor.
            if events_only and info.get('journal_id') and info.get('journal_cursor'):
                cursor = {'id': info['journal_id'], 'next': int(info['journal_cursor'])}
            self.store.finish_update(connection, mode, errors, cursor)
        summary = self.store.summary()
        return UpdateResult(mode, changed, summary['file_count'], summary['directory_count'],
                            time.perf_counter() - started, self.scanner.backend, errors)

    def mark_recycled(self, paths):
        with self.lock, self.store.connect() as connection:
            self.store.deactivate_paths(connection, paths)
            self.store.set_meta(connection, 'last_updated', time.time())

    def close(self):
        self.closed = True
        self.watcher.close()
