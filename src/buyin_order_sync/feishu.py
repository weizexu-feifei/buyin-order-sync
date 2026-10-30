"""User-authorized lark-cli adapter and read-before/write-after delta synchronization."""
from collections import Counter
from decimal import Decimal, InvalidOperation
import json
import os
from pathlib import Path
import shutil
import subprocess
import uuid

MONEY_FIELDS = {"成交金额", "平台补贴", "百应原退款金额", "退款平台补贴", "支付金额", "退款金额", "实际营业额", "预估佣金"}
COUNT_FIELDS = {"成交订单数", "退款订单数", "实际订单数", "成交件数"}
TEXT_FIELDS = ["同步键", "账号ID", "账号名称", "所属团队", "运营人员", "商品编号", "商品标题", "金额口径", "数据来源文件"]
DATE_FIELDS = {"出单日期", "数据截至"}
METADATA = {"数据截至", "数据来源文件"}


def schema():
    fields = [{"name": "同步键", "type": "text"}]
    fields += [{"name": n, "type": "text"} for n in TEXT_FIELDS if n != "同步键"]
    fields += [{"name": n, "type": "datetime", "style": {"format": "yyyy-MM-dd" if n == "出单日期" else "yyyy-MM-dd HH:mm"}} for n in sorted(DATE_FIELDS)]
    fields += [{"name": n, "type": "number", "style": {"type": "currency", "precision": 2, "currency_code": "CNY"}} for n in sorted(MONEY_FIELDS)]
    fields += [{"name": n, "type": "number", "style": {"type": "plain", "precision": 0}} for n in sorted(COUNT_FIELDS)]
    return fields


def normalize(name, value):
    if name in MONEY_FIELDS | COUNT_FIELDS:
        # Missing numbers must not silently equal zero.
        try:
            number = Decimal(str(value))
            if not number.is_finite(): raise ValueError("飞书数值无效")
            return number
        except (InvalidOperation, TypeError) as exc:
            raise ValueError("飞书数值缺失或无效：" + name) from exc
    if name in DATE_FIELDS:
        from .source import timestamp
        return timestamp(value).date().isoformat() if name == "出单日期" else timestamp(value).isoformat()
    return "" if value is None else str(value)


def same(expected, actual):
    return all(normalize(k, v) == normalize(k, actual.get(k)) for k, v in expected.items() if k not in METADATA)


def index_records(rows):
    result = {}
    for row in rows:
        if not row.get("record_id"):
            raise ValueError("飞书读回缺少record_id")
        fields = row.get("fields", row)
        key = fields.get("同步键")
        if not key:
            raise ValueError("目标表存在空同步键，停止自动写入")
        if key in result:
            raise ValueError("目标表存在重复同步键，停止自动写入")
        result[key] = {"record_id": row["record_id"], "fields": fields}
    return result


class Lark:
    def __init__(self, root, base_token, table_id):
        self.root = Path(root).resolve()
        self.base_token, self.table_id = base_token, table_id
        self.destination = base_token + "/" + table_id
        executable = shutil.which("lark-cli")
        if not executable:
            raise RuntimeError("未找到lark-cli，请按安装说明配置")
        # Use the official Node entry directly on Windows, avoiding shell quoting.
        entry = Path(executable).parent / "node_modules/@larksuite/cli/scripts/run.js"
        if os.name == "nt" and entry.is_file():
            node = shutil.which("node")
            if not node: raise RuntimeError("未找到Node.js")
            self.command = [node, str(entry)]
        elif os.name == "nt" and executable.lower().endswith((".cmd", ".bat")):
            raise RuntimeError("请使用npm安装的官方lark-cli；不通过shell执行未知脚本")
        else:
            self.command = [executable]

    def call(self, args, payload=None):
        self.root.mkdir(parents=True, exist_ok=True)
        path = None
        try:
            if payload is not None:
                path = Path("data/lark") / (uuid.uuid4().hex + ".json")
                (self.root / path).parent.mkdir(parents=True, exist_ok=True)
                (self.root / path).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
                args = [*args, "--json", "@" + path.as_posix()]
            env = dict(os.environ, LARKSUITE_CLI_NO_UPDATE_NOTIFIER="1", LARKSUITE_CLI_NO_SKILLS_NOTIFIER="1")
            result = subprocess.run([*self.command, *args, "--as", "user"], cwd=self.root,
                capture_output=True, timeout=180, env=env,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if result.returncode:
                # Raw stderr can contain credentials, links, account or order details.
                raise RuntimeError("飞书CLI请求失败；请在本机检查认证、权限与CLI版本，勿上传原始输出")
            data = json.loads(result.stdout.decode("utf-8-sig"))
            if not isinstance(data, dict) or data.get("ok") is not True:
                raise RuntimeError("飞书CLI返回格式不符合约定，停止")
            return data.get("data", data)
        finally:
            if path:
                (self.root / path).unlink(missing_ok=True)

    def verify_schema(self):
        data = self.call(["base", "+field-list", "--base-token", self.base_token,
                          "--table-id", self.table_id, "--limit", "200"])
        fields = data.get("fields", data.get("items", [])) if isinstance(data, dict) else data
        actual = {f.get("name"): f for f in fields}
        for field in schema():
            other = actual.get(field["name"])
            if not other or other.get("type") != field["type"]:
                raise ValueError("飞书字段缺失或类型不同：" + field["name"])

    def list(self, days):
        rows = []
        for day in sorted(set(days)):
            relative = Path("data/lark") / (uuid.uuid4().hex + ".ndjson")
            target = self.root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                filt = {"logic": "and", "conditions": [["出单日期", "==", f"ExactDate({day})"]]}
                data = self.call(["base", "+record-list", "--base-token", self.base_token,
                    "--table-id", self.table_id, "--filter-json", json.dumps(filt, ensure_ascii=False),
                    "--format", "ndjson", "--output", relative.as_posix()])
                if data.get("has_more") is not False or not target.is_file():
                    raise RuntimeError("飞书分页未读全，停止；请缩小单日业务量或完善分页适配")
                rows.extend(json.loads(line) for line in target.read_text(encoding="utf-8-sig").splitlines() if line.strip())
            finally:
                # CLI may also create a manifest beside the artifact. Only the
                # unpredictable filename prefix reserved for this query is removed.
                for artifact in target.parent.glob(target.stem + '*'):
                    if artifact.is_file(): artifact.unlink(missing_ok=True)
        return rows

    def create(self, fields):
        data = self.call(["base", "+record-batch-create", "--base-token", self.base_token,
                          "--table-id", self.table_id], {"create_records": fields})
        if data.get("ignored_fields"):
            raise RuntimeError("飞书忽略部分字段，需核查，停止")

    def update(self, fields):
        data = self.call(["base", "+record-batch-update", "--base-token", self.base_token,
                          "--table-id", self.table_id], {"update_records": fields})
        if data.get("ignored_fields"):
            raise RuntimeError("飞书忽略部分字段，需核查，停止")


def synchronize(store, remote, expected, days, dry_run=False):
    """Never delete remote rows; unresolved creates block all further writes."""
    remote.verify_schema()
    pending = store.pending(remote.destination) if store else []
    pending_days = [key.split("|", 1)[0] for _, keys in pending for key in keys]
    actual = index_records(remote.list([*days, *pending_days]))
    for batch_id, keys in pending:
        if any(key not in actual for key in keys):
            raise RuntimeError("前次新增结果待确认；停止自动重提，请人工核对飞书和本地批次")
        if not dry_run: store.confirm(batch_id)
    creates, updates, skipped = [], [], 0
    for key, fields in expected.items():
        row = actual.get(key)
        if row is None: creates.append(fields)
        elif not same(fields, row["fields"]): updates.append((row["record_id"], fields))
        else: skipped += 1
    stats = dict(created_rows=len(creates), updated_rows=len(updates), skipped_rows=skipped,
                 confirmed_rows=0, dry_run=dry_run)
    if dry_run: return stats
    if store is None: raise ValueError("真实同步必须使用持久批次记录")
    for offset in range(0, len(creates), 200):
        batch = creates[offset:offset+200]
        keys = [fields["同步键"] for fields in batch]
        batch_id = uuid.uuid4().hex
        store.begin_batch(batch_id, remote.destination, keys)
        try:
            remote.create(batch)
        except Exception as exc:
            # A response can be lost after acceptance. Preserve pending state if
            # readback is incomplete; never blindly submit a create a second time.
            checked = index_records(remote.list(sorted({key.split("|", 1)[0] for key in keys})))
            if not all(key in checked and same(expected[key], checked[key]["fields"]) for key in keys):
                raise RuntimeError("新增结果待确认，已保留批次，未自动重提") from exc
        checked = index_records(remote.list(sorted({key.split("|", 1)[0] for key in keys})))
        if not all(key in checked and same(expected[key], checked[key]["fields"]) for key in keys):
            raise RuntimeError("新增读回尚未确认，已保留批次")
        store.confirm(batch_id)
    for offset in range(0, len(updates), 200):
        remote.update(dict(updates[offset:offset+200]))
    final = index_records(remote.list(days))
    if any(key not in final or not same(fields, final[key]["fields"]) for key, fields in expected.items()):
        raise RuntimeError("飞书写后读回不一致，停止；下次先重新读取差异")
    stats["confirmed_rows"] = len(expected)
    return stats
