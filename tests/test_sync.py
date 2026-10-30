import copy
import json
from pathlib import Path
import tempfile
import unittest
from buyin_order_sync.feishu import index_records, same, synchronize
from buyin_order_sync.store import Store


def fields(key='2025-01-01|10000001|DEMO-PRODUCT-001', amount=200):
    return {'同步键': key, '出单日期': key.split('|')[0] + ' 00:00', '支付金额': amount,
            '退款金额': 0, '实际营业额': amount, '账号ID': '10000001', '数据来源文件': 'synthetic.xlsx'}


class FakeRemote:
    destination = 'synthetic-base/synthetic-table'
    def __init__(self):
        self.rows, self.create_calls, self.update_calls = [], 0, 0
        self.lose_create_response = self.drop_create = self.drop_update = False
    def verify_schema(self): pass
    def list(self, days):
        return copy.deepcopy([r for r in self.rows if r['fields']['同步键'].split('|')[0] in days])
    def create(self, records):
        self.create_calls += 1
        if not self.drop_create:
            self.rows += [{'record_id': f'demo-{len(self.rows)+i}', 'fields': copy.deepcopy(r)} for i, r in enumerate(records)]
        if self.lose_create_response: raise TimeoutError('synthetic timeout')
    def update(self, records):
        self.update_calls += 1
        if not self.drop_update:
            for row in self.rows:
                if row['record_id'] in records: row['fields'] = copy.deepcopy(records[row['record_id']])


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / 'synthetic.sqlite3')
        self.addCleanup(self.store.db.close)
        self.remote = FakeRemote()
        self.item = fields()
        self.expected = {self.item['同步键']: self.item}
        self.days = ['2025-01-01']

    def sync(self, dry=False): return synchronize(self.store, self.remote, self.expected, self.days, dry_run=dry)

    def test_repeat_same_source_skips(self):
        first, second = self.sync(), self.sync()
        self.assertEqual(first['created_rows'], 1)
        self.assertEqual(second, dict(created_rows=0, updated_rows=0, skipped_rows=1, confirmed_rows=1, dry_run=False))
        self.assertEqual((self.remote.create_calls, self.remote.update_calls), (1, 0))

    def test_business_change_updates_original_record(self):
        self.sync()
        self.item['退款金额'] = 200
        self.item['实际营业额'] = 0
        result = self.sync()
        self.assertEqual(result['updated_rows'], 1)
        self.assertEqual((len(self.remote.rows), self.remote.rows[0]['record_id']), (1, 'demo-0'))

    def test_file_name_alone_does_not_write(self):
        self.sync()
        self.item['数据来源文件'] = 'different-synthetic.xlsx'
        self.assertEqual(self.sync()['skipped_rows'], 1)

    def test_lost_create_response_is_read_back_not_reposted(self):
        self.remote.lose_create_response = True
        self.assertEqual(self.sync()['confirmed_rows'], 1)
        self.assertEqual(self.remote.create_calls, 1)
        self.assertEqual(self.store.pending(self.remote.destination), [])

    def test_unconfirmed_create_blocks_next_attempt(self):
        self.remote.drop_create = self.remote.lose_create_response = True
        with self.assertRaisesRegex(RuntimeError, '待确认'): self.sync()
        self.assertEqual(len(self.store.pending(self.remote.destination)), 1)
        with self.assertRaisesRegex(RuntimeError, '待确认'): self.sync()
        self.assertEqual(self.remote.create_calls, 1)

    def test_pending_from_previous_range_is_checked(self):
        key = '2024-12-01|10000001|DEMO-OLDER'
        self.store.begin_batch('synthetic-pending', self.remote.destination, [key])
        with self.assertRaisesRegex(RuntimeError, '待确认'): self.sync()
        self.assertEqual(self.remote.create_calls, 0)

    def test_pending_present_recovers_without_duplicate_create(self):
        self.store.begin_batch('synthetic-pending', self.remote.destination, [self.item['同步键']])
        self.remote.rows = [{'record_id': 'demo-existing', 'fields': copy.deepcopy(self.item)}]
        self.assertEqual(self.sync()['skipped_rows'], 1)
        self.assertEqual(self.store.pending(self.remote.destination), [])
        self.assertEqual(self.remote.create_calls, 0)

    def test_duplicate_remote_key_stops_before_mutation(self):
        self.remote.rows = [{'record_id': 'demo-1', 'fields': copy.deepcopy(self.item)}, {'record_id': 'demo-2', 'fields': copy.deepcopy(self.item)}]
        with self.assertRaisesRegex(ValueError, '重复'): self.sync()
        self.assertEqual(self.remote.create_calls, 0)

    def test_blank_remote_key_stops(self):
        with self.assertRaisesRegex(ValueError, '空同步键'): index_records([{'record_id': 'demo-1', 'fields': {}}])

    def test_missing_number_is_not_zero(self):
        with self.assertRaisesRegex(ValueError, '缺失'): same({'退款金额': 0}, {})

    def test_dry_run_does_not_write_or_clear_pending(self):
        self.assertEqual(self.sync(True)['created_rows'], 1)
        self.assertEqual(self.remote.create_calls, 0)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM batches').fetchone()[0], 0)

    def test_update_readback_failure_is_failure(self):
        self.sync()
        self.item['实际营业额'] = 199
        self.remote.drop_update = True
        with self.assertRaisesRegex(RuntimeError, '不一致'): self.sync()

    def test_timezone_dates_compare_semantically(self):
        self.assertTrue(same({'出单日期': '2025-01-01 00:00'}, {'出单日期': '2024-12-31T16:00:00Z'}))
