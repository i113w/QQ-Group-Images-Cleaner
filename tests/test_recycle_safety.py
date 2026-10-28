from dataclasses import replace
import os
import stat
import sys
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import lib.safe_recycle as safe
from tests.helpers import fixture, move_fixture, write_file


class RecycleSafetyTests(unittest.TestCase):
    def setUp(self):
        self.scope, self.root, self.outside = fixture(self._testMethodName)
        self.target = write_file(self.root / 'photo [1].jpg', b'preview target')
        self.other = write_file(self.outside / 'keep.jpg', b'keep outside')
        self.plan = safe.make_plan(self.root, [str(self.target)], 'Explicit test fixture preview')
        self.approval = safe.RecycleApproval(self.plan.id, self.plan.digest)

    def test_missing_approval_cannot_reach_recycling(self):
        with self.assertRaises(safe.RecycleSafetyError):
            safe.recycle(self.plan, None)
        self.assertEqual(self.target.read_bytes(), b'preview target')

    def test_approval_is_bound_to_exact_plan(self):
        changed = replace(self.plan, reason='Different target purpose')
        with self.assertRaises(safe.RecycleSafetyError):
            safe.validate_approval(changed, self.approval)
        other_plan = safe.make_plan(self.root, [str(self.target)], 'Another explicit test plan')
        with self.assertRaises(safe.RecycleSafetyError):
            safe.validate_approval(other_plan, self.approval)

    def test_relative_and_outside_paths_are_rejected(self):
        for path in ('photo [1].jpg', str(self.other)):
            with self.subTest(path=path):
                with self.assertRaises(safe.RecycleSafetyError):
                    safe.make_plan(self.root, [path], 'Test rejected path')
        self.assertEqual(self.other.read_bytes(), b'keep outside')

    def test_directories_are_rejected_and_brackets_are_literal(self):
        with self.assertRaises(safe.RecycleSafetyError):
            safe.make_plan(self.root, [str(self.root)], 'Test directory target')
        self.assertEqual(self.plan.targets[0].path, str(self.target))
        safe.validate_approval(self.plan, self.approval)

    def test_modified_file_requires_a_new_review(self):
        self.target.write_bytes(b'changed content')
        with self.assertRaises(safe.RecycleSafetyError):
            safe.validate_approval(self.plan, self.approval)
        self.assertEqual(self.target.read_bytes(), b'changed content')

    def test_cutoff_uses_current_file_time_instead_of_stale_index_time(self):
        current = time.localtime(self.plan.targets[0].real_time)
        with self.assertRaises(safe.NoRecycleTargets):
            safe.make_plan(self.root, [str(self.target)], 'Reject stale date candidate',
                           cutoff=(current.tm_year - 1, 12))
        accepted = safe.make_plan(self.root, [str(self.target)], 'Current date candidate',
                                 cutoff=(current.tm_year, current.tm_mon))
        self.assertEqual(accepted.targets, self.plan.targets)

    def test_replaced_file_cannot_reuse_prior_approval(self):
        archived = self.outside / self.target.name
        move_fixture(self.target, archived, self.scope)
        write_file(self.target, b'preview target')
        os.utime(self.target, ns=(self.target.stat().st_atime_ns, self.plan.targets[0].mtime_ns))
        with self.assertRaises(safe.RecycleSafetyError):
            safe.validate_approval(self.plan, self.approval)
        self.assertEqual(archived.read_bytes(), b'preview target')
        self.assertTrue(self.target.is_file())

    def test_reparse_target_is_rejected_before_any_file_operation(self):
        original = safe.os.stat
        def reparse(path, *args, **kwargs):
            if os.fspath(path) == str(self.target):
                return SimpleNamespace(st_mode=stat.S_IFREG, st_file_attributes=0x400)
            return original(path, *args, **kwargs)
        with patch.object(safe.os, 'stat', side_effect=reparse):
            with self.assertRaises(safe.RecycleSafetyError):
                safe.make_plan(self.root, [str(self.target)], 'Reject reparse point')
        self.assertTrue(self.target.is_file())

    @unittest.skipUnless(sys.platform == 'win32', 'Windows-only recycle capacity policy.')
    def test_insufficient_capacity_stops_before_creating_shell_operation(self):
        with patch.object(safe, 'recycle_capacity', return_value=0):
            with patch.object(safe.ole, 'CoCreateInstance') as create:
                with self.assertRaises(safe.RecycleSafetyError):
                    safe.recycle(self.plan, self.approval)
                create.assert_not_called()
        self.assertTrue(self.target.is_file())

    @unittest.skipUnless(sys.platform == 'win32', 'Windows-only COM safety sink.')
    def test_sink_vetoes_permanent_delete_requests(self):
        sink = safe.RecycleProgressSink(self.plan)
        result = safe._call(sink.pointer, 11, (safe.wintypes.DWORD, safe.ctypes.c_void_p), 0, None)
        self.assertEqual(result, safe.E_ABORT)
        self.assertIn('permanent', sink.stopped_reason)
        self.assertTrue(self.target.is_file())

    @unittest.skipUnless(sys.platform == 'win32', 'Windows-only COM safety sink.')
    def test_sink_checks_exact_current_target_and_stops_on_failure(self):
        sink = safe.RecycleProgressSink(self.plan)
        with patch.object(safe, '_shell_path', return_value=str(self.target)):
            result = safe._call(sink.pointer, 11, (safe.wintypes.DWORD, safe.ctypes.c_void_p), 0x80, None)
            self.assertEqual(result, safe.S_OK)
            failed = safe._call(sink.pointer, 12,
                (safe.wintypes.DWORD, safe.ctypes.c_void_p, safe.HRESULT, safe.ctypes.c_void_p),
                0x80, None, -1, None)
            self.assertEqual(failed, safe.E_ABORT)
            self.assertEqual(sink.recycled, [])
        self.assertEqual(self.target.read_bytes(), b'preview target')

    @unittest.skipUnless(sys.platform == 'win32', 'Windows-only COM safety sink.')
    def test_sink_rejects_unapproved_shell_target(self):
        sink = safe.RecycleProgressSink(self.plan)
        with patch.object(safe, '_shell_path', return_value=str(self.other)):
            result = safe._call(sink.pointer, 11, (safe.wintypes.DWORD, safe.ctypes.c_void_p), 0x80, None)
        self.assertEqual(result, safe.E_ABORT)
        self.assertTrue(self.target.is_file())
        self.assertTrue(self.other.is_file())


if __name__ == '__main__':
    unittest.main()
