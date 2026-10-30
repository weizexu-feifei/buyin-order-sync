import copy
from contextlib import redirect_stdout, redirect_stderr
from datetime import datetime
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from openpyxl import load_workbook
from buyin_order_sync.source import aggregate, cents, parse_export
from buyin_order_sync.store import Store
from buyin_order_sync.cli import check_snapshot, main

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('synthetic', ROOT / 'examples/generate_demo.py')
DEMO = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(DEMO)
CONFIG = json.loads((ROOT / 'config.example.json').read_text(encoding='utf-8'))
META = {'start': '2025-01-01', 'end': '2025-01-03', 'expected_rows': 5}


class SourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'synthetic.xlsx'
        DEMO.generate(self.path)

    def parse(self, meta=None):
        return parse_export(self.path, meta or META, CONFIG['accounts'])

    def edit(self, cell, value):
        book = load_workbook(self.path)
        book.active[cell] = value
        book.save(self.path)
        book.close()

    def test_amount_subsidy_and_full_refund(self):
        orders, info = self.parse()
        groups = list(aggregate(orders, CONFIG['accounts']).values())
        self.assertEqual((info['raw_rows'], info['selected_orders'], len(groups)), (5, 4, 4))
        self.assertEqual(sum(g['支付金额'] for g in groups), 530)
        self.assertEqual(sum(g['退款金额'] for g in groups), 200)
        self.assertEqual(sum(g['实际营业额'] for g in groups), 330)
        self.assertEqual(sum(g['预估佣金'] for g in groups), 16.5)
        self.assertEqual(sum(g['平台补贴'] for g in groups), 100)

    def test_refund_returns_to_original_payment_day(self):
        orders, _ = self.parse()
        refunded = next(o for o in orders if o['refund_count'])
        self.assertEqual(refunded['day'], '2025-01-01')
        self.assertEqual(refunded['refund'], refunded['amount'])
        self.assertEqual(refunded['refund_subsidy'], 5000)

    def test_unknown_account_is_not_zero(self):
        orders, info = self.parse()
        self.assertEqual(info['accounts_without_rows'], ['10000003'])
        self.assertNotIn('10000003', {o['source_id'] for o in orders})

    def test_account_nickname_does_not_control_identity(self):
        self.edit('E2', '完全不同的虚构昵称')
        self.assertEqual(len(self.parse()[0]), 4)

    def test_duplicate_order_stops(self):
        self.edit('A3', 'DEMO-ORDER-0001')
        with self.assertRaisesRegex(ValueError, '重复'): self.parse()

    def test_numeric_order_id_stops(self):
        self.edit('A2', int('1' * 16))
        with self.assertRaisesRegex(ValueError, '文本'): self.parse()

    def test_numeric_product_id_stops(self):
        self.edit('B2', 12345)
        with self.assertRaisesRegex(ValueError, '文本'): self.parse()

    def test_missing_amount_is_not_zero(self):
        self.edit('G2', None)
        with self.assertRaisesRegex(ValueError, '缺失'): self.parse()

    def test_negative_subsidy_stops(self):
        self.edit('H2', -1)
        with self.assertRaisesRegex(ValueError, '非负'): self.parse()

    def test_negative_commission_stops(self):
        self.edit('K2', -1)
        with self.assertRaisesRegex(ValueError, '非负'): self.parse()

    def test_partial_refund_unknown_status_stops(self):
        self.edit('D2', '部分退款')
        with self.assertRaisesRegex(ValueError, '状态'): self.parse()

    def test_wrong_rowcount_stops(self):
        with self.assertRaisesRegex(ValueError, '不完整'): self.parse(dict(META, expected_rows=4))

    def test_half_open_range(self):
        self.edit('J2', '2025-01-03 00:00:00')
        with self.assertRaisesRegex(ValueError, '范围'): self.parse()

    def test_quantity_rejects_fraction(self):
        self.edit('I2', 1.5)
        with self.assertRaisesRegex(ValueError, '整数'): self.parse()

    def test_money_rejects_nonfinite_or_precision(self):
        for value in ('NaN', 'Infinity', '0.001', None, '--', '-1'):
            with self.subTest(value=value), self.assertRaises(ValueError): cents(value)
        self.assertEqual(cents('1.23'), 123)

    def test_store_idempotent_and_status_update(self):
        orders, _ = self.parse()
        with Store(Path(self.temp.name) / 'orders.sqlite3') as store:
            self.assertEqual(store.ingest(orders, '2025-01-03'), 4)
            self.assertEqual(store.ingest(orders, '2025-01-03'), 0)
            revised = copy.deepcopy(orders)
            revised[0].update(status='订单退货退款', refund=20000, refund_count=1, refund_subsidy=5000, commission=0)
            self.assertEqual(store.ingest(revised, '2025-01-04'), 1)
            self.assertEqual(len(store.orders('2025-01-01', '2025-01-02')), 4)
            with self.assertRaisesRegex(ValueError, '较旧'): store.ingest(orders, '2025-01-03')

    def test_order_identity_change_rolls_back(self):
        orders, _ = self.parse()
        with Store(Path(self.temp.name) / 'orders.sqlite3') as store:
            store.ingest(orders)
            changed = copy.deepcopy(orders)
            changed[0]['product_id'] = 'OTHER-DEMO-ID'
            with self.assertRaisesRegex(ValueError, '改变'): store.ingest(changed)
            self.assertEqual(store.orders('2025-01-01', '2025-01-02'), sorted(orders, key=lambda o: (o['day'], o['order_id'])))

    def test_missing_snapshot_order_is_not_deleted(self):
        orders, _ = self.parse()
        with self.assertRaisesRegex(ValueError, '差异'): check_snapshot(orders[:-1], orders)

    def test_preview_never_writes_or_requests(self):
        args = ['--config', str(ROOT / 'config.example.json'), 'preview', str(self.path),
                '--start', '2025-01-01', '--end', '2025-01-03', '--as-of', '2025-01-03', '--expected-rows', '5']
        with patch('subprocess.run', side_effect=AssertionError('network/process forbidden')), patch('buyin_order_sync.cli.Store', side_effect=AssertionError('cache forbidden')), redirect_stdout(io.StringIO()):
            self.assertEqual(main(args), 0)

    def test_import_requires_raw_rowcount(self):
        config = Path(self.temp.name) / 'config.local.json'
        config.write_text(json.dumps(CONFIG), encoding='utf-8')
        with redirect_stdout(io.StringIO()):
            result = main(['--config', str(config), 'import', str(self.path), '--start', '2025-01-01', '--end', '2025-01-03', '--as-of', '2025-01-03'])
        self.assertEqual(result, 1)
        self.assertFalse((config.parent / '.local/data/last-success.json').exists())

    def test_import_repeat_refund_update_and_independent_check(self):
        from test_sync import FakeRemote
        remote = FakeRemote()
        config = Path(self.temp.name) / 'config.local.json'
        config.write_text(json.dumps(CONFIG), encoding='utf-8')
        common = [str(self.path), '--start', '2025-01-01', '--end', '2025-01-03',
                  '--as-of', '2025-01-03', '--expected-rows', '5']
        with patch('buyin_order_sync.cli.remote_for', return_value=remote), redirect_stdout(io.StringIO()):
            self.assertEqual(main(['--config', str(config), 'import', *common]), 0)
            self.assertEqual(main(['--config', str(config), 'import', *common]), 0)
            success = json.loads((config.parent / '.local/data/last-success.json').read_text(encoding='utf-8'))
            self.assertEqual((success['written_rows'], success['sync_stats']['skipped_rows']), (0, 4))
            self.edit('D2', '订单退货退款')
            self.edit('K2', 0)
            common[common.index('--as-of') + 1] = '2025-01-04'
            self.assertEqual(main(['--config', str(config), 'import', *common]), 0)
            self.assertEqual(main(['--config', str(config), 'check', *common]), 0)
        self.assertEqual(len(remote.rows), 4)
        self.assertEqual(remote.update_calls, 1)
