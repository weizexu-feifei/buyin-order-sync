"""Explicit entry points; preview never accesses a remote service."""
import argparse
from datetime import datetime, timedelta
import json
from pathlib import Path
import sys
import uuid
from .runtime_state import VERSION, RunJournal, error_code, inspect_schedule, safe_error, schedule_times
from .source import TZ, accounts_by_id, aggregate, parse_export, timestamp
from .store import Store, run_lock


def now(): return datetime.now(TZ)


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def load_config(path):
    path = Path(path).resolve()
    config = json.loads(path.read_text(encoding="utf-8-sig"))
    accounts_by_id(config.get("accounts", []))
    schedule_times(config)
    config["_config_path"] = str(path)
    config["_state_dir"] = str((path.parent / config.get("state_dir", ".local")).resolve())
    return config


def remote_for(config, root):
    from .feishu import Lark
    target = config.get("feishu", {})
    if not target.get("base_token") or not target.get("table_id"):
        raise ValueError("先填写自己的飞书base_token和table_id；离线检查请用preview")
    return Lark(root, target["base_token"], target["table_id"])


def meta_for(args):
    return {"start": timestamp(args.start).isoformat(), "end": timestamp(args.end).isoformat(),
            "expected_rows": args.expected_rows, "as_of": timestamp(args.as_of).isoformat(),
            "as_of_basis": "user_declared_snapshot_time", "file": str(Path(args.file).resolve())}


def check_snapshot(orders, cached):
    source = {o["order_id"]: o for o in orders}
    actual = {o["order_id"]: o for o in cached}
    differences = sum(source.get(key) != actual.get(key) for key in source.keys() | actual.keys())
    if differences:
        raise ValueError(f"原Excel与缓存有{differences}笔差异；未清理、未补0，停止同步")


def cached_in_range(store, meta):
    start, end = timestamp(meta["start"]), timestamp(meta["end"])
    return [o for o in store.orders(start.date().isoformat(), end.date().isoformat())
            if start <= timestamp(o["paid_at"]) < end]


def execute(args, config, root):
    from . import browser
    report = {"program_version": VERSION, "run_id": now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6],
        "started_at": now().isoformat(timespec="seconds"), "execution_origin": args.origin if args.command == "run" else "import",
        "status": "运行中", "orders_status": "not_started", "readback_status": "not_run",
        "reconciliation_status": "not_run", "written_rows": 0, "error": None}
    with run_lock(root):
        journal = RunJournal(root, report, write_json)
        try:
            if args.command == "run":
                if not args.sync: raise ValueError("自动导出必须显式加--sync；离线检查用preview")
                institution = str(config.get("institution", "")).strip()
                if not institution: raise ValueError("先填写自己有权访问的institution机构名称")
                journal.stage("打开百应并导出报表")
                report["schedule_check"] = inspect_schedule(config)
                end = now().replace(microsecond=0)
                lookback = config.get("lookback_days", 30)
                if type(lookback) is not int or not 1 <= lookback <= 90:
                    raise ValueError("lookback_days必须在1至90之间")
                start = timestamp(config["daily_start"]) if config.get("daily_start") else end.replace(day=1, hour=0, minute=0, second=0)
                start = max(start, (end - timedelta(days=lookback)).replace(hour=0, minute=0, second=0))
                if start >= end: raise ValueError("采集开始时间晚于当前时间")
                meta = browser.export_orders(root, start.isoformat(), end.isoformat(), institution,
                    config.get("max_rows", 200000), policy=config.get("export_policy"),
                    progress_callback=lambda stage: journal.stage(str(stage)))
                # Browser input has an inclusive second; parser uses [start,end).
                meta["end"] = (timestamp(meta["end"]) + timedelta(seconds=1)).isoformat()
            else:
                meta = meta_for(args)
                if meta["expected_rows"] is None:
                    raise ValueError("真实导入需要--expected-rows原始机构行数；不确定时先preview")
            journal.stage("校验已有Excel")
            orders, info = parse_export(meta["file"], meta, config["accounts"])
            if not orders: raise ValueError("目标账号没有订单返回，不能登记0或宣称成功")
            report.update(info, source_file=Path(meta["file"]).name, source_as_of=meta["as_of"],
                source_as_of_basis=meta["as_of_basis"], source_range=[meta["start"], meta["end"]],
                covered_accounts=info["observed_accounts"], coverage_note="无订单返回的账号保持未知，不生成零占位")
            if args.command == "run": browser.mark_export_validated(root, meta, meta["file"])
            with Store(root / "data/orders.sqlite3") as store:
                journal.stage("更新本地缓存")
                report["changed_orders"] = store.ingest(orders, meta['as_of'])
                mapping = accounts_by_id(config["accounts"])
                cached = [o for o in cached_in_range(store, meta) if o["source_id"] in mapping]
                check_snapshot(orders, cached)
                expected = aggregate(cached, config["accounts"])
                full_days = store.orders(timestamp(meta["start"]).date().isoformat(), timestamp(meta["end"]).date().isoformat())
                if {o["order_id"] for o in full_days if o["source_id"] in mapping} != {o["order_id"] for o in cached}:
                    raise ValueError("缓存含范围外同日订单，请使用完整日/月快照，停止覆盖")
                for fields in expected.values():
                    fields.update({"数据来源文件": report["source_file"], "数据截至": timestamp(meta["as_of"]).strftime("%Y-%m-%d %H:%M")})
                journal.stage("读取飞书差异并登记")
                from .feishu import synchronize
                report["orders_status"] = "syncing"
                report['written_rows'] = None  # Until readback, accepted writes may be unknown.
                report["sync_stats"] = synchronize(store, remote_for(config, root), expected, sorted({o["day"] for o in cached}))
                report["written_rows"] = report["sync_stats"]["created_rows"] + report["sync_stats"]["updated_rows"]
                report.update(status="成功", orders_status="success", readback_status="passed", ended_at=now().isoformat(timespec="seconds"))
                journal.stage("完成")
                write_json(root / "data/last-success.json", report)
                write_json(root / "data/last-order-success.json", report)
            return report, 0
        except Exception as exc:
            code = error_code(exc)
            report.update(status="需要重新登录" if code == "LOGIN_REQUIRED" else "失败",
                failure_stage=report.get("stage"), error_code=code, error=safe_error(exc),
                orders_status="failed", ended_at=now().isoformat(timespec="seconds"))
            journal.stage("失败，保留上次成功数据")
            return report, 1
        finally:
            journal.finish()


def parser():
    p = argparse.ArgumentParser(description="百应出单助手：本地核算、Excel导入与可选浏览器导出")
    p.add_argument("--config", default="config.local.json", help="本机私有配置，不提交Git")
    p.add_argument("--state-dir", help="私有运行目录，默认配置的state_dir")
    p.add_argument("--version", action="version", version=VERSION)
    sub = p.add_subparsers(dest="command", required=True)
    for name in ("preview", "import", "check"):
        s = sub.add_parser(name, help={"preview": "离线预览，不落库不联网", "import": "导入已有Excel并同步飞书", "check": "Excel/缓存/飞书只读核对"}[name])
        s.add_argument("file")
        s.add_argument("--start", required=True, help="北京时间起点（含）")
        s.add_argument("--end", required=True, help="北京时间终点（不含）")
        s.add_argument("--as-of", required=True, help="报表快照时间，导入时间不等于取数时间")
        s.add_argument("--expected-rows", type=int, help="原机构文件行数，含未选择账号")
    s = sub.add_parser("run", help="一次网页导出并同步；不注册自动任务")
    s.add_argument("--sync", action="store_true")
    s.add_argument("--origin", choices=("manual", "scheduled"), default="manual")
    sub.add_parser("login", help="在专用窗口自行登录，无自动填密码")
    sub.add_parser("ui", help="打开桌面界面；打开时不启动同步")
    s = sub.add_parser("init-feishu", help="创建自己的空Base，不自动修改配置")
    s.add_argument("--name", required=True)
    s.add_argument("--create-remote", action="store_true")
    s = sub.add_parser("export-worker", help=argparse.SUPPRESS)
    s.add_argument("--request", required=True)
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command == "export-worker":
            from .browser import export_worker
            if not args.state_dir: raise ValueError("内部worker缺少状态目录")
            return export_worker(Path(args.state_dir).resolve(), args.request)
        config = load_config(args.config)
        root = Path(args.state_dir or config["_state_dir"]).resolve()
        if args.command == "preview":
            orders, info = parse_export(args.file, meta_for(args), config["accounts"])
            print(json.dumps({**info, "summaries": list(aggregate(orders, config["accounts"]).values()),
                "note": "离线预览；未写缓存、未访问平台或飞书；无声明行数时不能证明文件完整"}, ensure_ascii=False, indent=2))
            return 0
        if args.command in ("import", "run"):
            report, code = execute(args, config, root)
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return code
        if args.command == "check":
            from .feishu import index_records, same
            meta = meta_for(args)
            orders, info = parse_export(args.file, meta, config["accounts"])
            if not info["rowcount_verified"]: raise ValueError("核验需要声明原始行数")
            if not orders: raise ValueError('目标账号没有返回订单，不能核验为0或成功')
            with Store(root / "data/orders.sqlite3", readonly=True) as store:
                cached = [o for o in cached_in_range(store, meta) if o["source_id"] in accounts_by_id(config["accounts"])]
                check_snapshot(orders, cached)
                expected = aggregate(orders, config["accounts"])
                remote = remote_for(config, root)
                remote.verify_schema()
                actual = index_records(remote.list(sorted({o["day"] for o in orders})))
                differences = sum(k not in actual or not same(v, actual[k]["fields"]) for k, v in expected.items())
                extra = sum(k not in expected for k in actual)
                result = {"destination": remote.destination, "cache_differences": 0,
                          "feishu_differences": differences, "extra_rows": extra,
                          "status": "passed" if differences == extra == 0 else "failed"}
                print(json.dumps(result, ensure_ascii=False, indent=2))
                return 0 if differences == extra == 0 else 1
        if args.command == "init-feishu":
            if not args.create_remote: raise ValueError("创建远端空表需显式加--create-remote")
            from .feishu import Lark, schema
            result = Lark(root, "", "").call(["base", "+base-create", "--name", args.name,
                "--table-name", "出单数据", "--time-zone", "Asia/Shanghai", "--fields", json.dumps(schema(), ensure_ascii=False)])
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if args.command == "login":
            from .browser import login
            institution = str(config.get("institution", "")).strip()
            if not institution: raise ValueError("先填写自己的institution机构名称")
            with run_lock(root): login(root, institution)
            return 0
        if args.command == "ui":
            if sys.platform != "win32": raise ValueError("桌面界面目前只支持Windows")
            from .desktop_ui import show_app
            show_app(root, lambda: load_config(args.config), schedule_times, now)
            return 0
    except Exception as exc:
        print("操作停止：" + safe_error(exc), file=sys.stderr)
        return 1
