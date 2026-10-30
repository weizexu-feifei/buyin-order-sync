import json
from pathlib import Path
from subprocess import CompletedProcess
import tempfile
import unittest
from unittest.mock import patch
from buyin_order_sync.feishu import Lark


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.adapter = Lark.__new__(Lark)
        self.adapter.root = Path(self.temp.name)
        self.adapter.command = ['synthetic-cli']
        self.adapter.base_token = 'synthetic-base'
        self.adapter.table_id = 'synthetic-table'

    def success(self, command, **kwargs):
        return CompletedProcess(command, 0, json.dumps({'ok': True, 'data': {'record_id_list': ['demo']}}).encode(), b'')

    def test_create_uses_official_shape_and_removes_payload(self):
        payloads = []
        def fake(command, **kwargs):
            path = self.adapter.root / command[command.index('--json') + 1][1:]
            payloads.append(json.loads(path.read_text(encoding='utf-8')))
            self.assertEqual(command[-2:], ['--as', 'user'])
            self.assertFalse(kwargs.get('shell', False))
            return self.success(command)
        with patch('subprocess.run', side_effect=fake): self.adapter.create([{'同步键': 'synthetic'}])
        self.assertEqual(payloads, [{'create_records': [{'同步键': 'synthetic'}]}])
        self.assertEqual(list((self.adapter.root / 'data/lark').glob('*')), [])

    def test_update_uses_record_id_map(self):
        def fake(command, **kwargs):
            path = self.adapter.root / command[command.index('--json') + 1][1:]
            self.assertEqual(json.loads(path.read_text(encoding='utf-8')), {'update_records': {'demo': {'支付金额': 200}}})
            return self.success(command)
        with patch('subprocess.run', side_effect=fake): self.adapter.update({'demo': {'支付金额': 200}})

    def list_result(self, has_more):
        def fake(command, **kwargs):
            path = self.adapter.root / command[command.index('--output') + 1]
            path.write_text(json.dumps({'record_id': 'demo', '同步键': '2025-01-01|10000001|DEMO'}, ensure_ascii=False) + '\n', encoding='utf-8')
            path.with_suffix('.manifest.json').write_text('{}', encoding='utf-8')
            return CompletedProcess(command, 0, json.dumps({'ok': True, 'data': {'has_more': has_more}}).encode(), b'')
        return fake

    def test_complete_manifest_and_cleanup(self):
        with patch('subprocess.run', side_effect=self.list_result(False)):
            rows = self.adapter.list(['2025-01-01'])
        self.assertEqual(rows[0]['record_id'], 'demo')
        self.assertEqual(list((self.adapter.root / 'data/lark').glob('*')), [])

    def test_more_pages_fail_closed(self):
        with patch('subprocess.run', side_effect=self.list_result(True)), self.assertRaisesRegex(RuntimeError, '分页'):
            self.adapter.list(['2025-01-01'])

    def test_raw_stderr_is_not_echoed(self):
        with patch('subprocess.run', return_value=CompletedProcess([], 1, b'', b'token=synthetic-private-value')):
            with self.assertRaises(RuntimeError) as error: self.adapter.call(['auth-probe'])
        self.assertNotIn('synthetic-private-value', str(error.exception))

    def test_non_ok_response_stops(self):
        with patch('subprocess.run', return_value=CompletedProcess([], 0, b'{"ok":false}', b'')):
            with self.assertRaises(RuntimeError): self.adapter.call(['synthetic'])
