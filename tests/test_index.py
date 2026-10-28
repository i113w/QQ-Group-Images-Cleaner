import os
from pathlib import Path
import struct
import threading
import time
import unittest
from unittest.mock import Mock, patch

from lib.file_metadata import FileRecord, path_key
from lib.index_service import IndexService
from lib.windows_native import IS_WINDOWS, NativeUnavailable, enumerate_directory, parse_notifications, parse_usn_records
from tests.helpers import fixture, force_directory_change, move_fixture, write_file


class IndexTests(unittest.TestCase):
    def setUp(self):
        self.scope, self.root, self.outside = fixture(self._testMethodName)
        self.left = self.root / 'left'
        self.right = self.root / 'right'
        self.first = write_file(self.left / '你好 [1].jpg', b'one')
        self.second = write_file(self.left / '100%_keep.jpg', b'two-two')
        self.third = write_file(self.right / 'notes.dat', b'three')
        self.service = IndexService(self.root, self.scope / 'indexes')
        self.addCleanup(self.service.close)
        self.service.refresh()

    def assert_totals(self, count, size):
        summary = self.service.store.summary()
        self.assertEqual(summary['file_count'], count)
        self.assertEqual(summary['total_size'], size)
        rows, total = self.service.store.query_files(limit=500)
        self.assertEqual(total, count)
        self.assertEqual(len(rows), count)
        self.assertEqual(sum(row['size'] for row in rows), size)

    def test_native_metadata_matches_stat(self):
        if not IS_WINDOWS:
            self.skipTest('Windows-only bulk metadata comparison.')
        listing = enumerate_directory(str(self.left))
        self.assertEqual({Path(row.path).name for row in listing.files}, {self.first.name, self.second.name})
        for record in listing.files:
            metadata = os.stat(record.path)
            self.assertEqual(record.size, metadata.st_size)
            self.assertEqual(record.mtime_ns, metadata.st_mtime_ns)
            self.assertEqual(record.atime_ns, metadata.st_atime_ns)
            self.assertEqual(record.ctime_ns, getattr(metadata, 'st_birthtime_ns', metadata.st_ctime_ns))
        self.assert_totals(3, 15)

    def test_cache_and_unchanged_refresh_never_enumerate_files(self):
        reopened = IndexService(self.root, self.scope / 'indexes')
        self.addCleanup(reopened.close)
        with patch.object(reopened.scanner, 'listing', side_effect=AssertionError('Unexpected directory scan')):
            with patch.object(reopened, '_journal', return_value=(None, None)):
                self.assertEqual(reopened.store.summary()['file_count'], 3)
                result = reopened.refresh()
                self.assertEqual(result.changed, 0)

    def test_online_add_updates_one_record_without_scanning_siblings(self):
        new_file = write_file(self.left / 'new.jpg', b'new-new')
        self.service.events.put(([(1, str(new_file))], False))
        with patch.object(self.service.scanner, 'listing', side_effect=AssertionError('Full directory scan')):
            result = self.service.refresh(events_only=True)
        self.assertEqual(result.changed, 1)
        self.assertEqual(result.mode, 'notifications')
        self.assert_totals(4, 22)

    def test_offline_add_checks_only_changed_directory(self):
        known = self.service.store.directories()
        write_file(self.left / 'offline.jpg', b'new')
        force_directory_change(self.left, known[path_key(self.left)]['mtime_ns'])
        visited = []
        original = self.service.scanner.listing
        def observe(path):
            visited.append(path_key(path))
            return original(path)
        with patch.object(self.service, '_journal', return_value=(None, None)):
            with patch.object(self.service.scanner, 'listing', side_effect=observe):
                self.service.refresh()
        self.assertEqual(visited, [path_key(self.left)])
        self.assert_totals(4, 18)

    def test_new_subdirectory_does_not_rescan_unchanged_subtrees(self):
        known = self.service.store.directories()
        folder = self.root / 'new folder'
        write_file(folder / 'new.jpg', b'new')
        force_directory_change(self.root, known[path_key(self.root)]['mtime_ns'])
        visited = []
        original = self.service.scanner.listing
        def observe(path):
            visited.append(path_key(path))
            return original(path)
        with patch.object(self.service, '_journal', return_value=(None, None)):
            with patch.object(self.service.scanner, 'listing', side_effect=observe):
                self.service.refresh()
        self.assertEqual(set(visited), {path_key(self.root), path_key(folder)})
        self.assert_totals(4, 18)

    def test_content_change_updates_size_and_aggregates(self):
        self.first.write_bytes(b'content changed')
        self.service.events.put(([(3, str(self.first))], False))
        with patch.object(self.service.scanner, 'listing', side_effect=AssertionError('Unexpected scan')):
            result = self.service.refresh(events_only=True)
        self.assertEqual(result.changed, 1)
        self.assert_totals(3, 27)

    def test_file_move_out_only_deactivates_metadata(self):
        destination = self.outside / self.first.name
        move_fixture(self.first, destination, self.scope)
        self.service.events.put(([(2, str(self.first))], False))
        self.service.refresh(events_only=True)
        self.assertIsNone(self.service.store.record(self.first))
        self.assertEqual(destination.read_bytes(), b'one')
        self.assert_totals(2, 12)
        with self.service.store.connect() as connection:
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM files WHERE present=0').fetchone()[0], 1)

    def test_directory_rename_preserves_count_and_rewrites_paths(self):
        destination = self.root / 'renamed left'
        move_fixture(self.left, destination, self.scope)
        self.service.events.put(([(4, str(self.left)), (5, str(destination))], False))
        self.service.refresh(events_only=True)
        self.assertIsNone(self.service.store.record(self.first))
        self.assertIsNotNone(self.service.store.record(destination / self.first.name))
        self.assert_totals(3, 15)

    def test_cancelled_rebuild_rolls_back_to_previous_complete_index(self):
        before = self.service.store.summary()
        cancelled = threading.Event()
        def cancel_after_first_batch(*_):
            cancelled.set()
        with self.assertRaises(InterruptedError):
            self.service.rebuild(progress=cancel_after_first_batch, cancel=cancelled)
        self.assertEqual(self.service.store.summary(), before)
        self.assertEqual(self.service.store.integrity_check(), 'ok')

    def test_unreadable_subtree_retains_prior_records_and_can_retry(self):
        original = self.service.scanner.listing
        def fail_left(path):
            if path_key(path) == path_key(self.left):
                raise PermissionError('Test-only inaccessible directory')
            return original(path)
        with patch.object(self.service.scanner, 'listing', side_effect=fail_left):
            result = self.service.refresh(force=True)
        self.assertEqual(len(result.errors), 1)
        self.assert_totals(3, 15)
        with patch.object(self.service, '_journal', return_value=(None, None)):
            recovered = self.service.refresh()
        self.assertEqual(recovered.errors, [])
        self.assertEqual(self.service.store.summary()['errors'], [])

    def test_search_treats_percent_underscore_and_brackets_literally(self):
        rows, total = self.service.store.query_files(search='%_')
        self.assertEqual(total, 1)
        self.assertEqual(rows[0]['display_path'], str(self.second))
        rows, total = self.service.store.query_files(search='[1]')
        self.assertEqual(total, 1)
        self.assertEqual(rows[0]['display_path'], str(self.first))

    def test_timestamp_bucket_changes_update_both_months_atomically(self):
        second = 1_000_000_000
        record = FileRecord(str(self.first), 3,
            int(time.mktime((2024, 6, 1, 0, 0, 0, 0, 0, -1))) * second,
            int(time.mktime((2022, 1, 1, 0, 0, 0, 0, 0, -1))) * second,
            int(time.mktime((2023, 8, 1, 0, 0, 0, 0, 0, -1))) * second)
        with self.service.store.connect() as connection:
            self.service.store.upsert_files(connection, [record], 99)
        rows, total = self.service.store.query_files(year=2022, month=1)
        self.assertEqual(total, 1)
        self.assertEqual(rows[0]['size'], 3)
        self.assert_totals(3, 15)

    def test_native_watcher_captures_new_files_without_elevation(self):
        if not IS_WINDOWS or not self.service.start_watching():
            self.skipTest('Directory notifications unavailable on this platform.')
        created = write_file(self.right / 'watcher.jpg', b'watched')
        deadline = time.monotonic() + 4
        while not self.service.pending and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertTrue(self.service.pending)
        self.service.refresh(events_only=True)
        self.assertIsNotNone(self.service.store.record(created))
        self.assert_totals(4, 22)

    def test_expired_journal_rechecks_content_changes_that_do_not_touch_parent(self):
        self.first.write_bytes(b'content changed')
        journal = Mock()
        journal.changes.side_effect = NativeUnavailable('Expired test journal cursor')
        with self.service.store.connect() as connection:
            self.service.store.set_meta(connection, 'journal_id', '1')
            self.service.store.set_meta(connection, 'journal_cursor', '5')
        with patch.object(self.service, '_journal', side_effect=[(journal, {'id': '1', 'next': 10}), (None, None)]):
            result = self.service.refresh()
        self.assertEqual(result.mode, 'full')
        journal.close.assert_called_once()
        self.assert_totals(3, 27)

    def test_notification_overflow_rechecks_all_content(self):
        self.first.write_bytes(b'content changed')
        self.service.events.put(([], True))
        result = self.service.refresh(events_only=True)
        self.assertEqual(result.mode, 'full')
        self.assert_totals(3, 27)


class NativeRecordTests(unittest.TestCase):
    def test_usn_v2_fields_and_unicode_filename(self):
        name = '你好.jpg'.encode('utf-16-le')
        length = (60 + len(name) + 7) // 8 * 8
        record = bytearray(length)
        struct.pack_into('<IHHQQqqIIIIHH', record, 0, length, 2, 0, 21, 33, 10, 20,
                         0x100, 0, 0, 0x20, len(name), 60)
        record[60:60 + len(name)] = name
        parsed = parse_usn_records(struct.pack('<q', 99) + record)
        self.assertEqual(parsed, [(f'{21:032x}', f'{33:032x}', '你好.jpg', 0x20, 0x100)])

    def test_corrupt_and_unsupported_usn_records_are_rejected(self):
        with self.assertRaises(OSError):
            parse_usn_records(struct.pack('<qIHH', 0, 99, 2, 0))
        with self.assertRaises(OSError):
            parse_usn_records(struct.pack('<qIHH', 0, 8, 4, 0))

    def test_usn_v3_supports_full_128_bit_file_ids(self):
        name = 'new.png'.encode('utf-16-le')
        length = (76 + len(name) + 7) // 8 * 8
        record = bytearray(length)
        struct.pack_into('<IHH', record, 0, length, 3, 0)
        record[8:24] = (2 ** 100 + 19).to_bytes(16, 'little')
        record[24:40] = (2 ** 110 + 29).to_bytes(16, 'little')
        struct.pack_into('<qqIIIIHH', record, 40, 10, 20, 0x100, 0, 0, 0x20, len(name), 76)
        record[76:76 + len(name)] = name
        parsed = parse_usn_records(struct.pack('<q', 99) + record)
        self.assertEqual(parsed[0][:3], (f'{2 ** 100 + 19:032x}', f'{2 ** 110 + 29:032x}', 'new.png'))

    def test_notifications_keep_literal_unicode_paths(self):
        name = '你好 [1].jpg'.encode('utf-16-le')
        data = struct.pack('<III', 0, 1, len(name)) + name
        result = parse_notifications(data, os.path.abspath('notification-fixture'))
        self.assertEqual(result[0][0], 1)
        self.assertEqual(Path(result[0][1]).name, '你好 [1].jpg')
        with self.assertRaises(OSError):
            parse_notifications(struct.pack('<III', 0, 1, 999), os.path.abspath('notification-fixture'))


if __name__ == '__main__':
    unittest.main()
