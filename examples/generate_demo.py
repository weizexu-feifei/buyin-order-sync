"""Generate a synthetic workbook from scratch. No production inputs are accepted."""
from datetime import datetime
from pathlib import Path
import io
import re
import zipfile
from openpyxl import Workbook

HEADERS = ["订单id", "商品id", "商品名称", "订单状态", "达人昵称", "达人抖音号",
           "成交金额", "平台补贴", "商品数量", "订单支付时间", "总佣金收入"]
ROWS = [
    ["DEMO-ORDER-0001", "DEMO-PRODUCT-001", "虚构商品甲", "订单付款", "示例账号A", "10000001", 200, 50, 1, "2025-01-01 10:00:00", 10],
    ["DEMO-ORDER-0002", "DEMO-PRODUCT-002", "虚构商品乙", "订单退货退款", "示例账号A", "10000001", 200, 50, 1, "2025-01-01 11:00:00", 0],
    ["DEMO-ORDER-0003", "DEMO-PRODUCT-003", "虚构商品丙", "订单收货", "示例账号B", "10000002", 100, 0, 2, "2025-01-01 12:00:00", 5],
    ["DEMO-ORDER-0004", "DEMO-PRODUCT-001", "虚构商品甲", "订单结算", "示例账号A", "10000001", 30, 0, 1, "2025-01-02 13:00:00", 1.5],
    ["DEMO-ORDER-0005", "DEMO-PRODUCT-004", "虚构商品丁", "订单付款", "未选示例账号", "19999999", 80, 10, 1, "2025-01-02 14:00:00", 4],
]


def generate(path):
    book = Workbook()
    book.properties.creator = "Example generator"
    book.properties.lastModifiedBy = "Example generator"
    book.properties.title = "Synthetic orders - no real business data"
    book.properties.created = book.properties.modified = datetime(2025, 1, 3)
    sheet = book.active
    sheet.title = "虚构订单"
    sheet.append(HEADERS)
    for row in ROWS: sheet.append(row)
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = sheet.dimensions
    book.save(path)
    book.close()
    # Spreadsheet writers stamp the current modification time during save.
    # Normalize both core XML and ZIP metadata; this is a purely synthetic fixture.
    original = Path(path).read_bytes()
    with zipfile.ZipFile(io.BytesIO(original)) as source, zipfile.ZipFile(path, 'w') as target:
        for name in source.namelist():
            data = source.read(name)
            if name == 'docProps/core.xml':
                data = re.sub(rb'(<dcterms:modified\b[^>]*>)[^<]+', rb'\g<1>2025-01-03T00:00:00Z', data)
            entry = zipfile.ZipInfo(name, (2025, 1, 3, 0, 0, 0))
            entry.compress_type = zipfile.ZIP_DEFLATED
            entry.external_attr = 0o100644 << 16
            target.writestr(entry, data)


if __name__ == "__main__":
    generate(Path(__file__).with_name("demo-orders.xlsx"))
