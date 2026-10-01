# Tiger 行情接入

`tiger_market` 是独立的数据检查模块，与 `tiger_paper` 下单模块分开登记。它检查当前凭据能读取哪些数据，并保存真实返回值。它不会生成目标仓位或下单，也不是完整的 `snapshot_data` 提供者。

在项目根目录运行，默认读取 `secrets/tiger_paper/`：

```text
.venv\Scripts\python.exe bot.py market-status
.venv\Scripts\python.exe bot.py market-status --connect --symbol AAPL --out runs/market-check-001
```

第一条只检查本地配置；第二条读取一只股票的实时报价、延迟行情、前复权和不复权日线、行业、TTM 营业利润，同时查询权限及历史数据额度。输出完整保存在新目录的 `market-status.json`；重复检查请使用新目录，旧证据不会被覆盖。数据目录应放在已被 Git 忽略的 `runs/` 下。

SDK 默认会抢占行情设备访问权，本模块明确关闭了这个默认行为。需要将行情访问权切到本程序时，显式使用：

```text
.venv\Scripts\python.exe bot.py market-status --connect --claim-device --out runs/market-check-002
```

这可能断开另一台 API 设备的行情连接。取得设备访问权不等于获得美股行情订阅，也不会购买服务。命令仅尝试一次，不会在失败后自动反复抢占。

## 如何看检查结果

- `checks.*.status`：`available` 表示该次请求返回数据；`empty` 表示没有数据；`failed` 表示请求失败。失败不以零值替代。
- `permissions` / `history_quota`：Tiger 返回的权限名及剩余额度；不是从模拟账户资金推断的权限。
- `daily_adjusted` / `daily_raw`：最多各 260 根日线，只检查一个明确选择的代码。原始返回保留当天可能尚未完成的 K 线，`completed_adjusted_bar_count` 只计纽约日期早于检查日的有效日线。
- `recent_two_sided_quote`：存在近期成交时间、正常状态和有效买卖价；只是诊断。Tiger 的 `latest_time` 是成交时间，不能单凭它证明盘口报价更新时间。
- `delayed`：延迟价格单独保存，永不替代实时买卖价。
- `strategy_ready` / `universe_complete`：在此诊断中固定为 `false`；一只股票的数据并不代表原策略的完整股票池已经就绪。

## 原策略接下来需要的数据

2026-10-02（澳洲时间）联网验证：取得设备访问权后，返回的权限列表只有 `aStockQuoteLv1`；AAPL 实时报价和 TTM 营业利润请求仍返回权限错误。AAPL 前复权及不复权日线各返回 260 条，其中 259 条的纽约日期早于检查日；延迟行情与行业分类可读取。查询时股票历史额度显示剩余 20。该结果仅描述当时的账户权限，后续应重新检查，不代表获得完整策略所需数据。

当前策略仍要求最多 5 只科技股、正的 TTM 营业利润、253 根已完成日线和成交额过滤。接入完整运行前，还需要：

1. 可用的美股报价权限和财务数据权限；核实足以覆盖股票池的历史行情额度。
2. 可核验的普通股、主上市、非 ADR 股票池及稳定证券标识。Tiger 科技行业列表可能含 OTC、ADR、失效代码，不能直接当作合格股票池；其行业分类也不等同于 QC 的 Morningstar。
3. 按真实可知时点冻结基本面与股票池，校验日历、缺失数据和复权口径，然后转换成 `us_technology_daily_v1` 快照。不得把今天获取的列表回填成昨天已知。
4. 核对 Tiger 模拟资金与框架单笔上限：例如 100 万美元账户的 19% 目标仓位可能超过现有 10 万美元单笔上限，运行前必须明确处理，不能自动提高上限。

数据权限不足时，可以独立更换数据供应商，继续用 Tiger 模拟券商执行；需先验证新数据源与回测的口径一致。检查模块保留原策略的盈利条件，不会为通过连接测试而删掉它。

官方参考：[行情权限与设备](https://docs.itigerup.com/docs/quote-common)、[股票行情](https://docs.itigerup.com/docs/quote-stock)、[财务数据权限 FAQ](https://docs.itigerup.com/docs/faq)。
