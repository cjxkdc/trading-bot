# Massive 数据接入

Massive 只登记为一个数据供应商。**策略声明需要什么，供应商负责获取和标准化。** 科技股、全行业、是否需要财报和历史长度由当前策略的需求决定，不需要切换到另一套 Massive 模块。

## 在 Portal 中使用

1. 打开根目录的 `Trading Bot.exe`，开启后台，在「策略库」选择策略并保存。
2. 「数据中心」选择 **Massive**。页面显示当前策略的股票范围、日线长度和所需字段。
3. 展开连接设置，填写 API Key 和套餐允许的请求速率，点「保存设置」。密钥保存在本机 `secrets/massive/connection.json`；切换策略共用这一份设置，不进入 Git、日志或命令参数。它不是加密保险箱，分享项目时不要附上 `secrets/`。
4. 点「检查数据权限」。只检查当前策略需要的接口：科技股盈利策略检查财报，MOM8 不检查或下载财报。某个必需字段不可用时停止，不会擅自删除筛选条件。
5. 点「下载并预览」，检查覆盖率、缺失数据和价格截止日期。长任务可停止，之后复用已成功取得的缓存；下载失败不会覆盖上次成功结果。

| 策略 | 请求范围 | 请求字段 |
|---|---|---|
| 科技股盈利动量 | 美国科技普通股 | 复权收盘价、行业、成交额；盈利筛选开启时加 TTM 营业利润 |
| MOM8 框架适配版 | 美国普通股全行业 | 复权收盘价、行业、市值、未复权股价、成交股数；不请求财报 |
| 科技股等权对照 | 美国科技普通股 | 复权收盘价、行业、成交额 |

历史长度从策略参数计算，不固定在供应商里。筛选、排名和停止补查的条件由策略提供纯函数；数据源不按策略名称写死选股规则。MOM8 的规则或参数变化后会重新筛选、生成新快照，已下载的原始行情仍可复用。切换范围、字段或历史长度后，旧需求的快照不会被当作当前需求的快照使用。

**行情按交易日批量下载，资料按需补查。** 一次批量日线请求取得当天美国市场的行情，再在本地保留策略股票池。原始日线配合截止信号日的拆股事件重建拆股复权价格和成交股数；同日多条拆股记录的股票单独读取供应商复权日线复核，不猜测复权顺序，不增加现金股息复权。

MOM8 先在完整价格股票池中检查价格、成交量和趋势，再按动量排名补查 SIC、市值和 FIGI，直到分散后的前 12 名缓冲确定，或全部候选已查完。不会只查前 8 只、任意截掉低排名候选，也不会把缺失资料填零。科技行业策略仍需 SIC 查询确认行业，仅对范围内股票查询所需财报。

日线按日期缓存并验证内容摘要：最近 7 天的缓存有效 24 小时，更早的原始行情缓存有效 90 天，随后重新验证供应商修订。基础资料沿用配置的有效期（默认 1 天），拆股和普通请求有效 24 小时；权限检查总是联网，缓存保留原观察时间。停止任务保留已完成日期；再次采集复用有效缓存，仅补齐缺失或过期部分。

默认限速仍为 5 次/分钟，不会自动改成付费速率。253 个交易日的首次日线下载理论上约 51 分钟，另加股票名单、拆股、候选资料和网络等待；缓存齐全后的采集主要是权限复核、新日期和资料更新。批量接口权限已经使用保存的 Key 联网检查；这不代表完整股票池采集或策略收益已经验收。

## 历史回填

「回测 → 历史回填」把同一套请求驱动的采集按日期重复执行：每个锚定日期（每月 1 日或每周一）生成一份快照，参考交易日截止到锚定日期的前一天，股票池列表与基础资料带供应商的 `date` 时点参数，财报按 `filing_date.lte` 截止到价格日期，日线仍走批量接口并复用缓存。结果写在 `runs/data/massive/requests/<请求指纹>/backfill-<日期>-<随机>/preview-data.json`，带 `as_of_requested` 字段和一条历史回填的限制说明；`historical_point_in_time` 仍为 `false`，不会被选为当前数据，也不能用于下单，只进入本地回放的快照序列。

队列状态保存在 `runs/portal/backfill.json`（`POST /api/backfill/plan` 估算，`POST /api/backfill` 开始，`/pause` `/resume` `/cancel` 控制）。工作台空闲时每次只采集一个日期；任一日期失败就整体暂停，避免权限或额度问题反复消耗请求；数据源、策略或策略参数（请求指纹）变化时也暂停。服务重启后未完成的日期重新排队，队列保持暂停等待确认。请求量估算：首个日期下载全部历史日线，之后每个日期只需新增交易日，再加每只候选股的资料和财报各一次；按 5 次/分钟的套餐，12 个月、每月约 120 只候选的回填需要数小时，已缓存的请求不再重复。

## CLI 与配置

在项目根目录的 Python 环境中运行：

```text
python -m app.market_data configure --source massive --requests-per-minute 5
python -m app.market_data status
python -m app.market_data status --connect
python -m app.market_data collect --source massive
```

`configure` 用隐藏输入读取 Key。没有本地连接配置时可读取 `MASSIVE_API_KEY`；存在文件时以文件为准。不要将 Key 写进公开配置。

- `strategies/plugins/*.json` 的 `data_requirements`：策略要求的范围和字段；历史长度来自策略的 `history_requirements`。
- `configs/data_sources.json`：供应商实例、连接和缓存路径，与券商配置独立。
- `configs/massive.json`：请求预算、缓存时间，以及把通用科技股范围转换成供应商 SIC 的映射。这里不选择当前策略或股票池。
- `data/massive_provider.py`：统一供应商实现；`data/request_control.py`：需求绑定、快照解析和校验。
- `data/massive_batch.py`：批量行情校验、日期缓存与拆股重建；`framework/collection.py`：纯策略采集钩子契约。

旧 `massive_technology`、`massive_momentum` 实现及配置仅保留兼容用途，不再作为 Portal 的独立选项。原 Portal 的旧选项会映射到 Massive，策略保持原样。原数据文件保留；新的请求缓存共用一个目录，旧快照不自动冒充新契约。

## 数据口径

| 数据 | 处理方式 |
|---|---|
| 股票池 | 当前美国普通股 `CS`，XNAS/XNYS/XASE、USD；根据策略需求保留科技行业或全行业 |
| 证券标识 | `share_class_figi`，缺失或重复列为数据问题 |
| 行业 | SIC 四位编码；科技行业映射沿用原配置，不等同于 QC Morningstar 分类 |
| 动量价格 | 已完成日线，拆股复权，不含股息复权 |
| 成交额 | 未复权收盘价 × 未复权成交股数，按所需天数保存 |
| 成交股数 | 原始成交量按窗口内拆股比例调整到末日股份单位，供 MOM8 流动性过滤 |
| 市值 | 当前参考资料，只在策略要求时使用；不能视为历史时点市值 |
| 日历 | 与 SPY 日线交易日对齐，排除纽约当天；缺失不填零、不向前填充 |
| 盈利 | TTM operating_income，核对 CIK/ticker、filing_date 和 period_end，拒绝超过 200 天的报告；不重复求和 |
| 可知时间 | 保留原始观察时间及财报申报日期，不将当前修订资料回填成历史已知资料 |
| 执行报价 | 不提供可执行 bid/ask，研究排名不能直接下单 |

策略选股、盈利、排名和仓位规则未变。Massive 的股票池、行业和复权口径与 QC 不同，结果不能直接宣称复现 QC 回测。

## 文件与失败处理

```text
runs/data/massive/
  cache/                         供应商请求缓存，跨策略复用
  requests/<需求摘要>/
    latest.json                  该需求最近一次成功预览
    <时间-标识>/preview-data.json
    <时间-标识>/progress.json
    <时间-标识>/failure.json      仅失败时存在
```

研究格式为 current_us_equity_request_v1，包含完整需求、需求摘要、来源配置、数据问题及排除项；固定 execution_allowed=false、historical_point_in_time=false。`coverage` 区分完整资料采集和已确定的排名前缀，并记录价格候选、资料已查及剩余数量；MOM8 文件只含选股所需的资料前缀，不能当作全市场资料数据库。超过大小或请求预算时停止，不截断股票池冒充完整结果。collection_complete=false 表示有股票数据缺失或其他数据问题。

研究文件不生成执行授权。QC 回测继续使用 QC 数据和现有策略导出；这次改动没有增加历史时点股票池、完整历史财报或联网订单执行链。

## 官方接口依据

- [REST 鉴权](https://massive.com/docs/rest/quickstart)：Bearer Header。
- [日线聚合](https://massive.com/docs/rest/stocks/aggregates/custom-bars)：拆股复权与原始日线。
- [市场批量日线](https://massive.com/docs/rest/stocks/aggregates/daily-market-summary)：按交易日取得市场行情。
- [拆股事件](https://massive.com/docs/rest/stocks/corporate-actions/splits)：原始拆股比例及执行日期。
- [Ticker 信息](https://massive.com/docs/rest/stocks/tickers/ticker-overview)：SIC、FIGI、CIK 等参考字段。
- [财报字段](https://massive.com/knowledge-base/article/what-fields-can-i-expect-from-massives-financials-api)：income-statements 接口、TTM。
- [财报 filing_date](https://massive.com/knowledge-base/article/does-massive-provide-the-filing-date-for-any-financial-reports)：不能以 period_end 代替可知时间。

数据命令连接已运行的工作台，沿用同一模块选择和任务记录。请先打开 Trading Bot.exe；命令返回任务编号后可在页面查看进度。
