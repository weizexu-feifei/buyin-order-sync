"""Local order identity, uncertain-write ledger, and process-scoped locking."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sqlite3


class Store:
    def __init__(self, path, readonly=False):
        self.path = Path(path).resolve()
        if readonly:
            self.db = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True)
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.db = sqlite3.connect(self.path)
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS orders(order_id TEXT PRIMARY KEY, day TEXT NOT NULL,
                    source_id TEXT NOT NULL, product_id TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS batches(batch_id TEXT PRIMARY KEY, destination TEXT NOT NULL,
                    keys_json TEXT NOT NULL, status TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS snapshots(source_id TEXT, day TEXT, as_of TEXT NOT NULL,
                    PRIMARY KEY(source_id,day));
            """)
            self.db.commit()

    def __enter__(self): return self
    def __exit__(self, *args): self.db.close()

    def ingest(self, orders, as_of=None):
        changed = 0
        with self.db:
            if as_of:
                from .source import timestamp
                snapshot = timestamp(as_of)
                if any(timestamp(o['paid_at']) > snapshot for o in orders):
                    raise ValueError('数据截至时间早于订单付款时间')
                for uid, day in {(o['source_id'], o['day']) for o in orders}:
                    previous = self.db.execute('SELECT as_of FROM snapshots WHERE source_id=? AND day=?', (uid, day)).fetchone()
                    if previous and timestamp(previous[0]) > snapshot:
                        raise ValueError('拒绝用较旧快照覆盖较新订单状态')
                    self.db.execute('INSERT OR REPLACE INTO snapshots VALUES(?,?,?)', (uid, day, snapshot.isoformat()))
            for order in orders:
                previous = self.db.execute("SELECT day,source_id,product_id,payload FROM orders WHERE order_id=?", (order["order_id"],)).fetchone()
                identity = (order["day"], order["source_id"], order["product_id"])
                if previous and tuple(previous[:3]) != identity:
                    raise ValueError("同一订单的付款日、账号ID或商品ID改变，停止入库")
                payload = json.dumps(order, ensure_ascii=False, sort_keys=True)
                if not previous or previous[3] != payload:
                    self.db.execute("INSERT OR REPLACE INTO orders VALUES(?,?,?,?,?)", (order["order_id"], *identity, payload))
                    changed += 1
        return changed

    def orders(self, start_day, end_day):
        return [json.loads(row[0]) for row in self.db.execute(
            "SELECT payload FROM orders WHERE day>=? AND day<=? ORDER BY day,order_id", (start_day, end_day))]

    def pending(self, destination):
        return [(row[0], json.loads(row[1])) for row in self.db.execute(
            "SELECT batch_id,keys_json FROM batches WHERE destination=? AND status='pending'", (destination,))]

    def begin_batch(self, batch_id, destination, keys):
        with self.db:
            self.db.execute("INSERT INTO batches VALUES(?,?,?,'pending')", (batch_id, destination, json.dumps(keys)))

    def confirm(self, batch_id):
        with self.db:
            self.db.execute("UPDATE batches SET status='confirmed' WHERE batch_id=?", (batch_id,))


@contextmanager
def run_lock(root):
    """OS releases this byte lock on process exit; stale files never mean stale locks."""
    path = Path(root) / "data/run.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    stream = path.open("a+b")
    acquired = False
    try:
        stream.seek(0)
        stream.write(b"0")
        stream.flush()
        stream.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        acquired = True
        yield
    except (BlockingIOError, PermissionError) as exc:
        if not acquired:
            raise RuntimeError("已有任务正在运行") from exc
        raise
    finally:
        if acquired:
            stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_UN)
        stream.close()
