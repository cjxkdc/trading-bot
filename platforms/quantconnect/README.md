# 科技股盈利动量 — QuantConnect 适配器 v0.4.0

## 使用

在项目根目录检查 `configs/portfolio_config.json`，然后：

```text
python bot.py export-qc --out runs/qc-baseline-001
```

将生成的 `main.py` 全部复制到 QC 新 Python 项目，再点击 Backtest。导出命令不会运行云端任务；`manifest.json` 记录配置和文件指纹，状态为 `exported_not_run`。生成文件不需要本地包。

也可以通过 `python tools/build_quantconnect.py` 更新本目录的默认可复制文件 `main.py`。改策略或参数后应重新生成，避免网站版本与本地代码不一致。

默认配置仍为 2020-01-01 至 2020-12-31，初始资金 100,000 美元，每笔 1 美元手续费和单边 10 bps 滑点。这些是研究假设，未按 Tiger/Futu 的实际收费校准。

## 当前版本状态

v0.1 曾在 2020-09-01 中断，v0.1.1 修复了基本面快照的日历处理。用户后来提供过完整年度报告。v0.2 引入共同决策与插件登记；v0.3 引入共享配置、策略声明的历史需求和平台目录拆分。

v0.4 增加结果导入/对比，以及可选的持仓名单输入。**v0.4 尚未重新进行云端回测。** 本地检查覆盖日期控制、输入与决策一致性，不等于完整 LEAN、供应商数据或策略收益验证。不能把旧报告归于新版。

运行后用 `bot.py import-qc` 导入下载的 JSON，具体命令见 [回测流程](../../backtests/README.md)。运行时日期以 `Research start/end` 输出，配置指纹写入 `RESEARCH_CONFIG` 日志；不依赖 QC 可能过滤掉的纯十六进制运行统计值。

## 当前默认规则

| 项目 | 规则 |
|---|---|
| 股票池 | 当时 Morningstar 科技板块普通股、主要股类，排除存托凭证；沿用 QC 基本面上市范围 |
| 盈利 | 当时已知的过去 12 个月营业利润大于 0，不是净利润或盈利预测 |
| 历史 | 默认 253 根完整日线，与 QQQ 交易日历对齐；策略改 lookback 时读取需求同步变化 |
| 流动性 | 最近 20 日原始收盘价 × 原始成交量，平均至少 1,000 万美元 |
| 动量 | 复权收盘价 P[t−21] / P[t−252] − 1 |
| 选股 | 分数由高到低，最多 5 只，同一 Morningstar industry 最多 2 只 |
| 仓位 | 每只目标 19%，最多 95%；空缺留现金，整股成交导致实际比例略有偏差 |
| 调仓 | 月末数据形成信号，下月首个交易日纽约 09:35 后调仓；落选退出，保留股票重新调整权重 |
| 账务 | 交易持仓使用 RAW；分红记现金、拆股改股数由 LEAN 处理，动量使用 ADJUSTED |
| 基准 | QQQ 复权价格基准，不代表实际持有 QQQ，也不是同股票池的对照组合 |

参数及风险上限由共享配置提供。当前 QC 接入负责科技板块数据；插件声明同一输入格式才可直接替换。策略类负责盈利、流动性和排名过滤；适配器不会按特定策略名称偷偷预筛股票。相较旧版本，这可能增加历史请求数量。

当前没有正动量门槛、止损或市场趋势开关。整体下跌时仍可能持有相对排名较高但收益为负的股票。科技板块分类不等同于所有通常被称为“科技巨头”的公司。

## 信息时间与交易处理

基本面使用月末最后交易日 00:00 ET 时已收到的最新合格快照；价格统计到该日收盘，下月首个交易日执行。这是保守约定：月末当日稍后发布的财报不用于这次调仓。基本面会在交易日之后的自然日到达，周末快照不能直接丢弃。

所有基本面复制为当时的数值，不保存会随时间改变的动态 getter。新股、停牌或缺失历史不允许静默缩短窗口。缺少及时快照时明确报错，不回填未来数据。

QC 批量目标下单先处理减仓。旧持仓到 10:00 仍无有效当分钟报价则停止；新标的缺报价则该槽位留现金。拒单会报错。程序拒绝 live_mode。

## 参数比较

默认 `MODE="configured"` 使用导出的配置。独立实验配置：

- `configs/momentum_only.json`：动量和流动性，去掉盈利门槛及额外行业限制。
- `configs/momentum_profit.json`：加盈利门槛。
- `configs/portfolio_config.json`：再加同一细分行业最多 2 只。

旧 MODE 的 momentum/profit/full 开关仅为兼容保留。手动改生成文件会使运行偏离 manifest，须另行记录；优先通过配置再导出。

## 与本地模拟的边界

本地与 QC 使用相同的选股源码、有效参数和目标仓位上限；相同标准输入得到相同决策记录。QC 由 LEAN 执行，本地则用报价回放与限价整股模拟。两者的成交、费用后仓位处理和报价时效检查不同，不能承诺同样收益。

QC 目前只支持固定每笔手续费；`fees.bps` 非零时导出会拒绝。QC 的滑点由回测配置控制，本地回放使用输入的 bid/ask，不额外叠加这个滑点参数。最大单笔金额等本地执行限制不是 LEAN 的执行逻辑。

## 文件关系

- `strategies/tech_profit_momentum.py`：独立选股规则。
- `framework/portfolio.py`、`decisions.py`：数据契约、目标检查、决策指纹。
- `adapter.py`：QC 历史数据、日历、订阅、成交模型、订单与日志。
- `main.py`：自动生成的可复制文件。
- `tools/build_quantconnect.py`：导出工具。
- `backtests/README.md`：实验记录与验证流程。

参考文档：
- [QC 基本面股票池](https://www.quantconnect.com/docs/v2/writing-algorithms/universes/equity/fundamental-universes)
- [历史数据请求](https://www.quantconnect.com/docs/v2/writing-algorithms/historical-data/history-requests)
- [复权和公司行动](https://www.quantconnect.com/docs/v2/writing-algorithms/securities/asset-classes/us-equity/requesting-data)
- [批量目标仓位](https://www.quantconnect.com/docs/v2/writing-algorithms/trading-and-orders/position-sizing)

