"""Strict Excel parsing and integer-cent arithmetic; no network or database writes."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

TZ = timezone(timedelta(hours=8))
ACTIVE = {"订单付款", "订单收货", "订单结算"}
REFUNDED = {"订单退货退款"}
AMOUNT_BASIS = "百应原成交金额计佣V3"
HEADERS = ["订单id", "商品id", "商品名称", "订单状态", "达人昵称", "达人抖音号",
           "成交金额", "平台补贴", "商品数量", "订单支付时间", "总佣金收入"]


def timestamp(value):
    result = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    return result.replace(tzinfo=TZ) if result.tzinfo is None else result.astimezone(TZ)


def cents(value):
    if value is None or str(value).strip() in ("", "-", "--"):
        raise ValueError("金额缺失，不能按0处理")
    try:
        number = Decimal(str(value).strip())
        if not number.is_finite() or number < 0 or number != number.quantize(Decimal(".01")):
            raise ValueError("金额必须是有效的非负分值")
        return int(number * 100)
    except (InvalidOperation, TypeError) as exc:
        raise ValueError("无法解析金额") from exc


def accounts_by_id(accounts):
    result = {}
    for account in accounts:
        uid = account.get("source_id")
        if not isinstance(uid, str) or not uid.strip() or uid != uid.strip() or any(c in uid for c in '|\r\n'):
            raise ValueError("每个账号必须配置精确文本source_id，不能按昵称匹配")
        if uid in result:
            raise ValueError("账号source_id重复")
        if not isinstance(account.get("name"), str) or not account["name"].strip():
            raise ValueError("账号名称缺失")
        result[uid] = account
    if not result:
        raise ValueError("至少配置一个账号")
    return result


def parse_export(path, meta, accounts):
    import openpyxl
    start, end = timestamp(meta["start"]), timestamp(meta["end"])
    if start >= end:
        raise ValueError("付款时间范围必须采用[start,end)，且start早于end")
    expected = meta.get("expected_rows")
    if expected is not None and (type(expected) is not int or expected < 0):
        raise ValueError("原始行数必须是非负整数")
    mapping = accounts_by_id(accounts)
    book = openpyxl.load_workbook(Path(path), read_only=True, data_only=True)
    selected, seen, observed = [], set(), 0
    try:
        if len(book.worksheets) != 1:
            raise ValueError("订单文件必须恰好包含一个工作表")
        sheet = book.active
        sheet.reset_dimensions()
        rows = sheet.iter_rows(values_only=True)
        header = [str(v).strip() if v is not None else "" for v in next(rows, ())]
        if not set(HEADERS).issubset(header) or any(header.count(h) != 1 for h in HEADERS):
            raise ValueError("订单文件核心字段缺失或重复：" + ",".join(sorted(set(HEADERS) - set(header))))
        for line, values in enumerate(rows, 2):
            if not any(v is not None for v in values):
                continue
            observed += 1
            raw = dict(zip(header, values))
            uid_value = raw["达人抖音号"]
            if isinstance(uid_value, float):
                raise ValueError(f"第{line}行账号ID是浮点数，可能丢失精度")
            uid = str(uid_value).strip() if uid_value is not None else ""
            if uid not in mapping:
                continue
            try:
                order, product = raw["订单id"], raw["商品id"]
                if not isinstance(order, str) or not isinstance(product, str):
                    raise ValueError("订单和商品ID必须是文本，避免丢失精度")
                order, product = order.strip(), product.strip()
                if any(c in order + product for c in '|\r\n'):
                    raise ValueError('ID含同步键分隔符或换行符')
                if not order or not product or order in seen:
                    raise ValueError("订单ID为空或重复")
                seen.add(order)
                status = str(raw["订单状态"]).strip()
                if status not in ACTIVE | REFUNDED:
                    raise ValueError("未识别订单状态；部分退款等状态需人工核验")
                paid = timestamp(raw["订单支付时间"])
                if not start <= paid < end:
                    raise ValueError("订单付款日期超出声明范围")
                amount, subsidy = cents(raw["成交金额"]), cents(raw["平台补贴"])
                commission = cents(raw["总佣金收入"])
                quantity = Decimal(str(raw["商品数量"]))
                if not quantity.is_finite() or quantity < 1 or quantity != quantity.to_integral_value():
                    raise ValueError("商品数量必须为正整数")
                title = str(raw["商品名称"] or "").strip()
                if not title:
                    raise ValueError("商品名称缺失")
                refunded = status in REFUNDED
                selected.append(dict(order_id=order, product_id=product, source_id=uid,
                    product_name=title, day=paid.date().isoformat(), paid_at=paid.isoformat(),
                    status=status, amount=amount, subsidy=subsidy, refund=amount if refunded else 0,
                    refund_subsidy=subsidy if refunded else 0, refund_count=int(refunded),
                    quantity=int(quantity), commission=commission))
            except (ValueError, InvalidOperation, TypeError) as exc:
                raise ValueError(f"第{line}行校验失败：{exc}") from exc
    finally:
        book.close()
    if expected is not None and observed != expected:
        raise ValueError(f"订单文件不完整：声明{expected}条，实际{observed}条")
    observed_ids = {o["source_id"] for o in selected}
    return selected, {"raw_rows": observed, "selected_orders": len(selected),
                      "observed_accounts": sorted(observed_ids),
                      "accounts_without_rows": sorted(set(mapping) - observed_ids),
                      "rowcount_verified": expected is not None}


def aggregate(orders, accounts):
    mapping = accounts_by_id(accounts)
    groups = {}
    for order in orders:
        uid = order["source_id"]
        if uid not in mapping:
            continue
        key = f"{order['day']}|{uid}|{order['product_id']}"
        account = mapping[uid]
        if key not in groups:
            groups[key] = {"同步键": key, "出单日期": order["day"] + " 00:00",
                "账号ID": uid, "账号名称": account["name"], "所属团队": account.get("team", ""),
                "运营人员": account.get("operator", ""), "商品编号": order["product_id"],
                "商品标题": order["product_name"], "金额口径": AMOUNT_BASIS,
                **{name: 0 for name in ("成交金额", "平台补贴", "百应原退款金额", "退款平台补贴",
                    "支付金额", "退款金额", "实际营业额", "预估佣金", "成交订单数", "退款订单数", "实际订单数", "成交件数")}}
        g = groups[key]
        # Iterate in deterministic order; the last title is presentation only.
        g["商品标题"] = order["product_name"]
        for field, value in (("成交金额", order["amount"]), ("支付金额", order["amount"]),
            ("平台补贴", order["subsidy"]), ("百应原退款金额", order["refund"]),
            ("退款金额", order["refund"]), ("退款平台补贴", order["refund_subsidy"]),
            ("实际营业额", order["amount"] - order["refund"]), ("预估佣金", order["commission"])):
            g[field] += value  # All money stays in integer cents until serialization.
        g["成交订单数"] += 1
        g["退款订单数"] += order["refund_count"]
        g["实际订单数"] += 1 - order["refund_count"]
        g["成交件数"] += order["quantity"]
    money = ("成交金额", "平台补贴", "百应原退款金额", "退款平台补贴", "支付金额", "退款金额", "实际营业额", "预估佣金")
    for g in groups.values():
        for field in money:
            g[field] = g[field] / 100
    return dict(sorted(groups.items()))
