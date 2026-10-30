# 百应出单助手 · Buyin Order Sync

把百应机构订单报表核算成「付款日 × 账号ID × 商品ID」汇总，保存在本机，并按差异登记到你自己的飞书多维表格。适合需要统一订单、退款和佣金口径的电商团队。

**公开候选版 v0.1.0 / Alpha。** 只包含通用源码、空白配置和虚构订单；没有真实账号、企业表格、订单、登录状态或历史记录。公开版本尚未完成跨机构实际接入验证。当前建议先用 Excel 导入；浏览器自动导出属于实验功能，页面变化或登录失效时会停止，需要人工处理。

## 能做什么

- 按精确账号ID选单，不依赖昵称；订单ID去重，金额以整数分计算。
- 退款追溯原付款日；保留成交原金额、平台补贴、退款及总佣金收入，便于核对。
- SQLite 本地缓存；飞书按差异新增/更新，未变化记录跳过；写后读取确认。
- 新增响应丢失时保留待确认批次，先查后写，无法确认时停止。
- 提供只读预览、三方核对、Windows 桌面界面和可选定时任务脚本。
- 无遥测、无外部AI调用、无自动付费重试。基础离线预览不需要飞书或平台登录。

## 先试虚构订单（不联网）

Windows PowerShell，Python 3.11 或 3.12：

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe -m buyin_order_sync --config config.example.json preview examples/demo-orders.xlsx --start 2025-01-01 --end 2025-01-03 --as-of 2025-01-03 --expected-rows 5
```

预期：原文件5行，选中4笔，4条汇总；支付金额530元，退款金额200元，实际营业额330元，预估佣金16.50元。第5笔属于未选择账号，会被排除；示例账号C没有返回，保持未知，不能说0单。

## 接入自己的飞书

1. 将 `config.example.json` 复制为 **`config.local.json`**，填写自己的账号ID、名称、分组及运营人员。`source_id` 对应原报表的「达人抖音号」，保留文本。不要上传这个私有配置。
2. 安装 Node.js 和官方 `@larksuite/cli`。本适配器按 CLI **1.0.94** 的命令契约实现，升级后先复验；不自动安装或重授权。
3. 执行 `lark-cli auth status`。已有有效 user 授权直接复用；没有时，按官方 CLI 引导授权你自己有权使用的飞书账号和 Base 权限。
4. 按 [字段说明](docs/fields.md) 建立专用空表，或显式运行下面的建表命令；命令会创建远端Base，返回地址/ID后手工填入私有配置的 `feishu`，不修改其他已有表。

```powershell
npm install -g @larksuite/cli@1.0.94
.\.venv\Scripts\python.exe -m buyin_order_sync --config config.local.json init-feishu --name "订单汇总测试" --create-remote
```

已有 Base 请单独建一个空的出单表，字段与 `schemas/order-fields.json` 一致。**不要直接指向带有人工业务记录的旧表**；本版需要独占管理「同步键」，发现重复或空键会停止。

## 导入真实报表

先离线 `preview`，确认时间范围、账号和金额，再执行 `import`。以下日期和行数仅为示例，需要替换成你自己的报表事实。原始行数指整份机构报表，不是选中账号的订单数。

```powershell
.\.venv\Scripts\python.exe -m buyin_order_sync --config config.local.json import "D:\private\orders.xlsx" --start 2025-01-01 --end 2025-01-03 --as-of "2025-01-03T08:00:00+08:00" --expected-rows 5
.\.venv\Scripts\python.exe -m buyin_order_sync --config config.local.json check "D:\private\orders.xlsx" --start 2025-01-01 --end 2025-01-03 --as-of "2025-01-03T08:00:00+08:00" --expected-rows 5
```

时间采用北京时间，范围是 `[start,end)`；导入完整自然日时，end 填下一天00:00。`--as-of` 是报表快照时间，不能填成后来的导入时间冒充新取数。`import` 会写本地缓存及飞书；`check` 不写业务数据，会产生短暂的本地CLI查询文件并自动清除。

## 可选：浏览器导出和桌面界面

仅支持 Windows、已安装 Google Chrome、有合法机构访问权限的用户。普通网页流程，不重放接口、不绕验证码。安装浏览器依赖后，先填写自己的 `institution`；首次使用用户自行登录专用窗口。

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[browser]"
.\.venv\Scripts\python.exe -m buyin_order_sync --config config.local.json login
.\.venv\Scripts\python.exe -m buyin_order_sync --config config.local.json run --sync --origin manual
.\.venv\Scripts\python.exe -m buyin_order_sync --config config.local.json ui
```

打开界面不触发同步。点击「立即更新」会真实导出并同步，点击登录会打开专用窗口。界面显示配置中的计划时间；**只有另行安装Windows任务后才会自动运行**。

`daily_start=null` 表示本月1日，`lookback_days=30` 限制回看范围；如需覆盖上个月仍在退款的订单，明确填写更早的daily_start，并自行安排月结核对。范围外历史不会自动刷新。超过平台上限会停止，不自动拆批增加导出频率。

安装计划任务前修改私有配置里的 `schedule`，示例为09:00和18:00。执行 `scripts/install_schedule.ps1` 会创建独立任务 **BuyinOrderSync**；若同名任务已存在则停止，避免覆盖其他任务。电脑需开启、当前用户已登录且网络可用。详见 [运行限制](docs/limitations.md)。

## 金额怎么算

| 场景 | 支付金额 | 退款金额 | 实际营业额 |
|---|---:|---:|---:|
| 报表成交200，补贴50，未退款 | 200 | 0 | 200 |
| 同一笔订单被标记为整单退货退款 | 200 | 200 | 0 |

补贴留底，不能再扣一次；整单退款按原成交计佣基数冲回。这里的「实际营业额」是本工具的**退款后计佣成交金额**，不能当作消费者现金实付、到账现金、利润或财务收入。预估佣金直接汇总Excel「总佣金收入」；不按比例重算，也不再扣一次退款。当前仅支持整单退款，不猜测部分退款的含义。

## 参与完善

欢迎修复Bug、补充虚构测试数据、改进安装文档和跨环境适配。贡献前请看 [CONTRIBUTING.md](CONTRIBUTING.md)；公开Issue只接受脱敏文字和虚构样例。不要上传真实订单、配置、日志、Cookie或截图。需要改变财务口径时先发Issue说明证据与预期，避免默默改变历史数据含义。

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

本仓库不附带任何第三方账号、授权或服务配额。使用者负责确认自己有权处理报表、访问表格并遵守平台规则。依赖与验证范围见 [docs/verification.md](docs/verification.md)，数据处理见 [PRIVACY.md](PRIVACY.md)。使用 MIT License；项目为社区工具，与相关平台无隶属关系。
