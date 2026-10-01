# 平台接入模块

平台层负责把外部世界转换成框架的数据类型。数据源和下单券商可以分别选择，例如未来由一个供应商提供数据、由 Tiger 或 Futu 执行订单。

## 当前已有

| 模块 | 登记类型 | 功能 |
|---|---|---|
| `data/snapshot.py` | snapshot_file | 读取标准股票快照，返回 MarketSnapshot |
| `data/csv.py` | csv | 读取单标的总回报数据，供 research.py 使用 |
| `data/tiger_market.py` | tiger_market / market_reader | Tiger 权限、单只股票报价/历史/基本面检查，保存诊断；不产生可交易快照 |
| `brokers/paper.py` | paper | 专用本地报价回放账户，立即全额模拟成交 |
| `brokers/tiger_paper.py` | tiger_paper / broker | Tiger 模拟账户、USD 整股限价单、查询/撤销/恢复；拒绝实盘 |
| `brokers/tiger_readonly.py` | tiger_readonly / account_reader | Tiger 配置检查与账户、持仓、未完成订单只读查询 |
| `quantconnect/adapter.py` | 通过 export-qc 选择 | QC 历史数据、日历、LEAN 撮合和日志 |

Tiger 模拟账户已完成联网查询、提交与撤单验证；当前默认券商操作选择 tiger_paper。paper 仍是独立本地回放。实盘下单、Futu/Moomoo 接入未实现。见 [券商接入说明](brokers/README.md)。

## 数据接口

数据模块实现 `load() -> MarketSnapshot`，返回股票快照、执行时点和买卖报价。供当前策略使用的格式为 `us_technology_daily_v1`。文件示例可用 `bot.py demo` 生成。

历史输入应为当时可知的科技股股票池和完整交易日序列。要求 `universe_complete=true` 是提供者声明；程序不能凭此证明没有幸存者偏差。基本面时间、退市、停牌、复权口径和真实行情权限仍由数据提供者校验。

联网数据检查使用 `market-status --connect --out runs/market-check-001`，与券商连接单独选择。Tiger 请求受数据权限与额度限制；见 [行情接入说明](data/README.md)。

## 券商接口

订单与账户契约在 `framework/contracts.py`、`framework/trading.py`：

- `account()`：账户、币种、可用现金、持仓、时间戳。
- `open_orders()`：未解决的订单。
- `submit(order)`：提交带唯一 client_id 的订单意图。
- `query(client_id)`：恢复时查询已有订单，结果不确定不能盲目重发。
- `cancel(broker_id)`：请求撤单；撤单确认不等于不会再成交。
- `close()`：释放连接。

连接器负责生命周期，实现 `mode`、`state_files`、`initialize()`、`connect()`；参考 `brokers/paper.py` 的 PaperConnector。broker_paper 模式另外提供 `execution_time()` 取得当前时间，不能把快照的历史时间当作发送订单的现在。框架中的策略永远不接收 SDK 客户端或账户凭据。

新增实现后，在 `plugins/` 登记入口，再修改配置的 `broker.type`。新券商适配需验证账户归属、权限、USD 现金、费用、整股、订单历史与成交状态。当前执行器的纸面执行限制不能通过把真实连接伪装为 paper 绕过。

## 可替换的范围

支持既定契约的模块可以换，不需要修改选股框架。新资产、新订单类型、异步实盘状态、结算或多币种要求，可能需要扩展通用框架，再由各平台实现；不能承诺任意券商或产品只改一行配置即可工作。

旧的 `platforms/paper.py`、`snapshot_data.py`、`csv_data.py` 仅作导入兼容，实际实现放在新子目录。

