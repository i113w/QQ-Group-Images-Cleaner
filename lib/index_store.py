"""Persistent SQLite metadata. Removing an index row never removes a file."""

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time

from lib.file_metadata import IMAGE_EXTENSIONS, path_key


FILE_UPSERT = '''
INSERT INTO files VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
ON CONFLICT(path) DO UPDATE SET
 display_path=excluded.display_path, parent=excluded.parent, name=excluded.name,
 size=excluded.size, ctime_ns=excluded.ctime_ns, mtime_ns=excluded.mtime_ns,
 atime_ns=excluded.atime_ns, real_time=excluded.real_time,
 year=excluded.year, month=excluded.month, extension=excluded.extension,
 present=1, seen=excluded.seen
'''
DIRECTORY_UPSERT = '''
INSERT INTO directories VALUES (?,?,?,?,?,?,?,?)
ON CONFLICT(path) DO UPDATE SET
 display_path=excluded.display_path, parent=excluded.parent,
 mtime_ns=excluded.mtime_ns, ctime_ns=excluded.ctime_ns,
 file_id=excluded.file_id, present=1, seen=excluded.seen
'''


class IndexStore:
    SCHEMA_VERSION = '2'
    SORTS = {
        'time_desc': 'real_time DESC, path', 'time_asc': 'real_time ASC, path',
        'size_desc': 'size DESC, path', 'size_asc': 'size ASC, path',
        'name': 'name COLLATE NOCASE, path',
    }

    def __init__(self, root, cache_dir):
        self.root = os.path.abspath(os.fspath(root))
        self.root_key = path_key(root)
        self.root_prefix = self.root.rstrip('\\/') + os.sep
        cache_dir = Path(cache_dir).absolute()
        cache_dir.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256((self.root_key + '\n' + self.SCHEMA_VERSION).encode('utf-8')).hexdigest()[:24]
        self.db_path = cache_dir / (digest + '.sqlite3')
        with self.connect() as connection:
            connection.executescript('''
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL) WITHOUT ROWID;
            CREATE TABLE IF NOT EXISTS files (
                path TEXT PRIMARY KEY, display_path TEXT NOT NULL, parent TEXT NOT NULL,
                name TEXT NOT NULL, size INTEGER NOT NULL,
                ctime_ns INTEGER NOT NULL, mtime_ns INTEGER NOT NULL, atime_ns INTEGER NOT NULL,
                real_time REAL NOT NULL, year INTEGER NOT NULL, month INTEGER NOT NULL,
                extension TEXT NOT NULL, present INTEGER NOT NULL, seen INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS directories (
                path TEXT PRIMARY KEY, display_path TEXT NOT NULL, parent TEXT NOT NULL,
                mtime_ns INTEGER NOT NULL, ctime_ns INTEGER NOT NULL,
                file_id TEXT NOT NULL, present INTEGER NOT NULL, seen INTEGER NOT NULL
            ) WITHOUT ROWID;
            CREATE TABLE IF NOT EXISTS months (
                year INTEGER NOT NULL, month INTEGER NOT NULL,
                file_count INTEGER NOT NULL, total_size INTEGER NOT NULL,
                PRIMARY KEY(year, month)
            ) WITHOUT ROWID;
            ''')
            version = self._meta(connection, 'schema')
            if version is not None and version != self.SCHEMA_VERSION:
                raise ValueError('Unsupported index schema; keep the old database and use a new cache folder.')
            saved_root = self._meta(connection, 'root')
            if saved_root is not None and saved_root != self.root_key:
                raise ValueError('Index root does not match the selected folder.')
            self.set_meta(connection, 'schema', self.SCHEMA_VERSION)
            self.set_meta(connection, 'root', self.root_key)
            if self._meta(connection, 'ready') == '1':
                self.ensure_indexes(connection)

    @staticmethod
    def ensure_indexes(connection):
        # For a new database, bulk-load first and build secondary indexes once.
        # Existing cached indexes remain available throughout later rebuilds.
        for statement in (
            'CREATE INDEX IF NOT EXISTS files_parent ON files(parent, present)',
            'CREATE INDEX IF NOT EXISTS files_month_time ON files(present, year, month, real_time DESC)',
            'CREATE INDEX IF NOT EXISTS files_time ON files(present, real_time DESC, path)',
            'CREATE INDEX IF NOT EXISTS directories_parent ON directories(parent, present)',
        ):
            connection.execute(statement)

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(str(self.db_path), timeout=15)
        connection.row_factory = sqlite3.Row
        # PERSIST keeps the rollback journal in place instead of unlinking it.
        # All application files/caches are retained; no cleanup/delete is needed.
        connection.execute('PRAGMA journal_mode=PERSIST')
        connection.execute('PRAGMA synchronous=NORMAL')
        connection.execute('PRAGMA temp_store=MEMORY')
        connection.execute('PRAGMA cache_size=-65536')
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _meta(connection, key, default=None):
        row = connection.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
        return row[0] if row else default

    @staticmethod
    def set_meta(connection, key, value):
        connection.execute('INSERT INTO meta VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                           (key, str(value)))

    def _key(self, path):
        relative = self._relative(path) if os.path.isabs(path) else os.path.normpath(path)
        return os.path.normcase(relative)

    def _relative(self, path):
        path = os.fspath(path)
        if path == self.root:
            return '.'
        if path.startswith(self.root_prefix):
            return path[len(self.root_prefix):] or '.'
        relative = os.path.relpath(path, self.root)
        if relative == '..' or relative.startswith('..' + os.sep):
            raise ValueError('The metadata path is outside the indexed root.')
        return relative

    def file_row(self, record, generation):
        relative = self._relative(record.path)
        parent = os.path.dirname(relative) or '.'
        stamp = record.real_time
        try:
            local = time.localtime(stamp)
        except (OverflowError, OSError, ValueError):
            local = time.localtime(0)
        return (os.path.normcase(relative), relative, os.path.normcase(parent), os.path.basename(relative),
                record.size, record.ctime_ns, record.mtime_ns, record.atime_ns,
                stamp, local.tm_year, local.tm_mon, os.path.splitext(relative)[1].lower(), 1, generation)

    def directory_row(self, record, generation):
        relative = self._relative(record.path)
        parent = os.path.dirname(relative) or '.'
        if relative == '.':
            parent = '..'
        return (os.path.normcase(relative), relative, os.path.normcase(parent),
                record.mtime_ns, record.ctime_ns, record.file_id, 1, generation)

    def _decode(self, row):
        row = dict(row)
        row['display_path'] = os.path.normpath(os.path.join(self.root, row['display_path']))
        row['path'] = path_key(row['display_path'])
        row['parent'] = path_key(os.path.dirname(row['display_path']))
        return row

    def info(self):
        with self.connect() as connection:
            return {row['key']: row['value'] for row in connection.execute('SELECT * FROM meta')}

    @property
    def ready(self):
        with self.connect() as connection:
            return self._meta(connection, 'ready') == '1'

    def summary(self):
        with self.connect() as connection:
            months = [dict(row) for row in connection.execute(
                'SELECT * FROM months WHERE file_count>0 ORDER BY year DESC, month DESC')]
            return {'months': months, 'file_count': sum(row['file_count'] for row in months),
                    'total_size': sum(row['total_size'] for row in months),
                    'directory_count': int(self._meta(connection, 'directory_count', '0')),
                    'last_updated': float(self._meta(connection, 'last_updated', '0')),
                    'mode': self._meta(connection, 'mode', ''),
                    'errors': json.loads(self._meta(connection, 'errors', '[]')),
                    'ready': self._meta(connection, 'ready') == '1'}

    def directories(self, connection=None):
        if connection is not None:
            rows = [self._decode(row) for row in connection.execute('SELECT * FROM directories WHERE present=1')]
            return {row['path']: row for row in rows}
        with self.connect() as connection:
            return self.directories(connection)

    @staticmethod
    def adjust_months(connection, deltas):
        connection.executemany('''
            INSERT INTO months VALUES (?,?,?,?) ON CONFLICT(year,month) DO UPDATE SET
            file_count=file_count+excluded.file_count, total_size=total_size+excluded.total_size
            ''', [(year, month, counts[0], counts[1])
                  for (year, month), counts in deltas.items() if counts != [0, 0]])

    @staticmethod
    def _delta(deltas, year, month, count, size):
        totals = deltas.setdefault((year, month), [0, 0])
        totals[0] += count
        totals[1] += size

    def upsert_files(self, connection, records, generation):
        """Update only changed file records and adjust aggregates in the same transaction."""
        rows, deltas = [], {}
        for record in records:
            row = self.file_row(record, generation)
            old = connection.execute(
                'SELECT size,ctime_ns,mtime_ns,atime_ns,year,month,present,display_path FROM files WHERE path=?',
                (row[0],)).fetchone()
            if old and old['present'] and (
                old['size'], old['ctime_ns'], old['mtime_ns'], old['atime_ns'], old['display_path']) == (
                    row[4], row[5], row[6], row[7], row[1]):
                continue
            if old and old['present']:
                self._delta(deltas, old['year'], old['month'], -1, -old['size'])
            self._delta(deltas, row[9], row[10], 1, row[4])
            rows.append(row)
        connection.executemany(FILE_UPSERT, rows)
        self.adjust_months(connection, deltas)
        return len(rows)

    def deactivate_paths(self, connection, paths):
        deltas, keys = {}, []
        for path in set(map(self._key, paths)):
            old = connection.execute('SELECT size,year,month FROM files WHERE path=? AND present=1',
                                     (path,)).fetchone()
            if old:
                self._delta(deltas, old['year'], old['month'], -1, -old['size'])
                keys.append((path,))
        connection.executemany('UPDATE files SET present=0 WHERE path=?', keys)
        self.adjust_months(connection, deltas)
        return len(keys)

    def deactivate_subtree(self, connection, path):
        key = self._key(path)
        prefix = key + os.sep
        condition = '1' if key == '.' else '(path=? OR substr(path,1,?)=?)'
        parameters = () if key == '.' else (key, len(prefix), prefix)
        deltas = {}
        for row in connection.execute(
                'SELECT year,month,COUNT(*) AS count,SUM(size) AS size FROM files WHERE present=1 AND ' +
                condition + ' GROUP BY year,month', parameters):
            self._delta(deltas, row['year'], row['month'], -row['count'], -row['size'])
        changed = -sum(values[0] for values in deltas.values())
        connection.execute('UPDATE files SET present=0 WHERE ' + condition, parameters)
        connection.execute('UPDATE directories SET present=0 WHERE ' + condition, parameters)
        self.adjust_months(connection, deltas)
        return changed

    def reconcile_listing(self, connection, listing, generation):
        key = self._key(listing.directory.path)
        existing_files = {row[0] for row in connection.execute(
            'SELECT path FROM files WHERE parent=? AND present=1', (key,))}
        actual_files = {self._key(record.path) for record in listing.files}
        changed = self.deactivate_paths(connection, existing_files - actual_files)
        changed += self.upsert_files(connection, listing.files, generation)
        existing_children = {row[0] for row in connection.execute(
            'SELECT path FROM directories WHERE parent=? AND present=1', (key,))}
        actual_children = {self._key(record.path) for record in listing.children}
        for child in existing_children - actual_children:
            changed += self.deactivate_subtree(connection, child)
        connection.execute(DIRECTORY_UPSERT, self.directory_row(listing.directory, generation))
        return changed

    def finalize_full(self, connection, generation, errors):
        # Preserve prior records under unreadable subtrees. Never interpret an
        # access error as evidence that those files disappeared.
        condition, parameters = 'seen<>?', [generation]
        for failed in errors:
            key = self._key(failed['path'])
            prefix = key + os.sep
            if key == '.':
                condition += ' AND 0'
            else:
                condition += ' AND NOT (path=? OR substr(path,1,?)=?)'
                parameters.extend((key, len(prefix), prefix))
        connection.execute('UPDATE files SET present=0 WHERE ' + condition, parameters)
        connection.execute('UPDATE directories SET present=0 WHERE ' + condition, parameters)
        connection.execute('UPDATE months SET file_count=0,total_size=0')
        connection.execute('''
            INSERT INTO months SELECT year,month,COUNT(*),SUM(size) FROM files
            WHERE present=1 GROUP BY year,month
            ON CONFLICT(year,month) DO UPDATE SET
            file_count=excluded.file_count,total_size=excluded.total_size
            ''')

    def finish_update(self, connection, mode, errors=(), journal=None):
        self.set_meta(connection, 'ready', '1')
        self.set_meta(connection, 'last_updated', time.time())
        self.set_meta(connection, 'mode', mode)
        self.set_meta(connection, 'errors', json.dumps(list(errors), ensure_ascii=False))
        count = connection.execute('SELECT COUNT(*) FROM directories WHERE present=1').fetchone()[0]
        self.set_meta(connection, 'directory_count', count)
        if journal:
            self.set_meta(connection, 'journal_id', journal['id'])
            self.set_meta(connection, 'journal_cursor', journal['next'])
        else:
            self.set_meta(connection, 'journal_id', '')
            self.set_meta(connection, 'journal_cursor', '')

    def query_files(self, *, year=None, month=None, search='', images_only=False,
                    sort='time_desc', limit=50, offset=0):
        clauses, parameters = ['present=1'], []
        if year is not None:
            clauses.append('year=?')
            parameters.append(int(year))
        if month is not None:
            clauses.append('month=?')
            parameters.append(int(month))
        if search:
            pattern = '%' + search.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'
            clauses.append("(? || display_path) LIKE ? ESCAPE '\\'")
            parameters.extend((self.root + os.sep, pattern))
        if images_only:
            clauses.append('extension IN (' + ','.join('?' for _ in IMAGE_EXTENSIONS) + ')')
            parameters.extend(sorted(IMAGE_EXTENSIONS))
        where = ' AND '.join(clauses)
        order = self.SORTS.get(sort, self.SORTS['time_desc'])
        with self.connect() as connection:
            if not search and not images_only:
                total_where, total_parameters = [], []
                if year is not None:
                    total_where.append('year=?')
                    total_parameters.append(year)
                if month is not None:
                    total_where.append('month=?')
                    total_parameters.append(month)
                total_sql = 'SELECT COALESCE(SUM(file_count),0) FROM months'
                if total_where:
                    total_sql += ' WHERE ' + ' AND '.join(total_where)
                total = connection.execute(total_sql, total_parameters).fetchone()[0]
            else:
                total = connection.execute('SELECT COUNT(*) FROM files WHERE ' + where, parameters).fetchone()[0]
            rows = connection.execute('SELECT * FROM files WHERE ' + where + ' ORDER BY ' + order +
                                      ' LIMIT ? OFFSET ?', [*parameters, max(1, min(500, int(limit))),
                                                            max(0, int(offset))])
            return [self._decode(row) for row in rows], total

    def files_before(self, year, month):
        with self.connect() as connection:
            return [self._decode(row) for row in connection.execute('''
                SELECT * FROM files WHERE present=1 AND (year<? OR (year=? AND month<=?))
                ORDER BY year,month,path
                ''', (year, year, month))]

    def record(self, path):
        with self.connect() as connection:
            row = connection.execute('SELECT * FROM files WHERE path=? AND present=1',
                                     (self._key(path),)).fetchone()
            return self._decode(row) if row else None

    def integrity_check(self):
        with self.connect() as connection:
            return connection.execute('PRAGMA integrity_check').fetchone()[0]
