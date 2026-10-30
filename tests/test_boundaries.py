from pathlib import Path
import os
import tempfile
import unittest
from unittest.mock import patch
from buyin_order_sync import browser
from buyin_order_sync.runtime_state import export_policy, safe_error, schedule_times


class BoundaryTests(unittest.TestCase):
    def test_export_retry_limit(self):
        self.assertEqual(export_policy()['download_attempts'], 2)
        with self.assertRaises(ValueError): export_policy({'export_policy': {'download_attempts': 3}})

    def test_schedule_validation(self):
        self.assertEqual(schedule_times({'schedule': ['18:00', '09:00']}), ['09:00', '18:00'])
        for times in ([], ['9:00'], ['09:00', '09:00'], ['24:00']):
            with self.subTest(times=times), self.assertRaises(ValueError): schedule_times({'schedule': times})

    def test_error_hides_urls_and_credentials(self):
        error = safe_error('failure https://example.invalid/private token=synthetic-secret')
        self.assertNotIn('https://', error)
        self.assertNotIn('synthetic-secret', error)

    def test_worker_rejects_external_request_path(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(Exception): browser.export_worker(Path(folder), Path(folder) / 'outside.json')

    def test_worker_requires_own_parent_chain(self):
        with patch.object(browser.os, 'getppid', return_value=200):
            self.assertTrue(browser.worker_parent_matches({'parent_pid': 200}))
            self.assertFalse(browser.worker_parent_matches({'parent_pid': 100, 'launcher_pid': 300}))
            if os.name == 'nt':
                with patch.object(browser, 'windows_parent_pid', return_value=100):
                    self.assertTrue(browser.worker_parent_matches({'parent_pid': 100, 'launcher_pid': 200}))
                with patch.object(browser, 'windows_parent_pid', return_value=999):
                    self.assertFalse(browser.worker_parent_matches({'parent_pid': 100, 'launcher_pid': 200}))

    def test_corrupt_download_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'synthetic.xlsx'
            path.write_bytes(b'not-a-workbook')
            with self.assertRaises(Exception): browser.validate_download_file(path)

    def test_view_distinguishes_unknown_and_zero_writes(self):
        from buyin_order_sync.desktop_ui import view_model
        success = {'status': '成功', 'selected_orders': 4, 'covered_accounts': ['10000001'],
                   'written_rows': 0, 'sync_stats': {'confirmed_rows': 4, 'skipped_rows': 4}}
        vm = view_model(success, {})
        self.assertEqual((vm['orders'], vm['summaries']), ('4', '4'))
        failed = view_model({'status': '失败', 'written_rows': None}, success)
        self.assertEqual(failed['orders'], '4')
        self.assertEqual(failed['tone'], 'red')
