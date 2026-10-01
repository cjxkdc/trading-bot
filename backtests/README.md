# 回测放在哪里

回测属于项目的研究运行方式，与实盘共同使用 `strategies/` 中的选股代码。

| 代码 | 职责 |
|---|---|
| `framework/backtest.py` | 已有的单标的 CSV 研究模型、成本和收益统计 |
| `research.py` | 单标的候选筛选、冻结、验证和最终测试 |
| `platforms/quantconnect/adapter.py` | 多股票历史数据、交易日历和 LEAN 回测接入 |
| `tools/build_quantconnect.py` | 将配置、策略和框架合成可复制的 QC 文件 |
| `platforms/quantconnect/results.py` | 解析 QC JSON，明确指标单位与回测完成状态 |
| `framework/experiments.py` | 保存证据、检查文件变化、生成比较报告；不依赖 QC SDK |
| `runs/` | 每次实验独立保存的导出文件、报告和日志；不提交到账户代码 |

## 当前科技股策略

先检查 `portfolio_config.json` 的策略、日期、资金和成本，再运行：

```text
python bot.py export-qc --out runs/qc-baseline-001
```

在 QuantConnect 新建 Python 项目，把 `runs/qc-baseline-001/main.py` 全部复制进去，再点击 Backtest。导出命令不会登录网站或运行云端回测。输出目录必须是新的；`manifest.json` 记录配置和代码校验值，初始状态为 `exported_not_run`。

已生成的默认可复制文件也位于 `platforms/quantconnect/main.py`。以后改参数优先修改配置后重新导出，避免只改网站代码导致本地策略与回测失去对应关系。

比较版本时分别生成新目录：

```text
python bot.py export-qc --config configs/momentum_only.json --out runs/qc-momentum-001
python bot.py export-qc --config configs/momentum_profit.json --out runs/qc-profit-001
```

默认仍为 2020 年复现区间，已看过这段结果，不能称作独立样本外测试。新增年份、成本假设、调参和结论都应单独记录。

## 保存回测证据

在 QC 回测结果的 Overview 中下载 JSON 结果；从该次运行的 Code 标签保存 `main.py`，不要使用之后改过的编辑器代码。然后：

```text
python bot.py import-qc --experiment runs/qc-baseline-001 --result downloaded-result.json --code downloaded-main.py
```

命令返回 `result_dir`，例如 `runs/qc-baseline-001/result-...`。保存原始 JSON、导出代码与配置、所提供的运行代码及校验值，不覆盖旧导出。支持 QC 下载 JSON、ReadBacktest API JSON 和 LEAN 结果包；PDF/订单 CSV 不能代替结果 JSON。

`--code` 可省略，但会标为 `source_unverified`。有代码副本且一致时标为 `supplied_code_matches_export`：仅证明提供的副本与导出一致，没有联网认证这份代码是否对应这份报告。代码或历史日期冲突时拒绝导入。不要把旧版的 140% 报告导入当前版本。

完成状态分为 `reported_complete`、`failed`、`incomplete`、`unverified`。没有任务完成元数据的下载文件保持 `unverified`，不根据漂亮收益或 `progress=1` 自行判定成功。检查 QC 状态及完整日志；文件中的状态也不等于策略值得实盘。

总收益读取 `Statistics.Net Profit`（百分比），年化读取 `Compounding Annual Return`；界面顶部 `RuntimeStatistics.Net Profit` 的美元金额不是这两个指标。缺失值保留 unknown。

## 分区间与成本压力比较

```text
python bot.py export-qc --out runs/qc-period-001 --start 2022-01-01 --end 2023-12-31 --note "Observe a different market period; no parameter changes"
python bot.py export-qc --out runs/qc-cost-001 --start 2022-01-01 --end 2023-12-31 --slippage-bps 30 --note "Same strategy and period, triple the slippage assumption"
```

这些命令只准备代码。分别在 QC 实际运行并导入结果后，再比较两次返回的目录：

```text
python bot.py compare-qc --results runs/qc-period-001/result-REPLACE-ID runs/qc-cost-001/result-REPLACE-ID --out runs/comparison-001
```

用实际 `result_dir` 替换示例中的 `result-REPLACE-ID`。输出 `comparison.md` 与 `comparison.json`，列出区间、成本、总收益、年化、回撤、Sharpe、订单数和证据状态。修改过的原始文件或汇总指标会拒绝比较。

比较不同策略时固定日期、资金和成本；成本实验只改成本。不同区间分别展示，不能按原始总收益直接排名。新日期不会自动成为未见样本：应先记录哪些数据已参与调参，再确定验证区间。当前不自动执行参数搜索或认定任何策略通过实盘门槛。

检查订单/交易明细、日志、QQQ 对照及收益是否集中在少数股票上仍需单独分析。这版汇总器不进行收益归因、统计显著性检验或完整滚动验证。

v0.4 本地检查覆盖报告核验与模块行为；尚未运行新的云端历史回测。

格式依据：[QC 回测结果](https://www.quantconnect.com/docs/v2/cloud-platform/backtesting/results)、[ReadBacktest API](https://www.quantconnect.com/docs/v2/cloud-platform/api-reference/backtest-management/read-backtest/backtest-statistics)。

## 回测和本地模拟的区别

QuantConnect/LEAN 负责历史撮合、分红和拆股。本地 `paper` 只是对文件中的报价立即模拟成交，不含连续历史、结算、公司行动或盘口，因此不能用 demo 的账户变化声称历史收益。

两者共用选股规则、参数和目标仓位上限；交易时点、费用处理、整股撮合、执行限制仍需分别核验。当前 QC 导出只支持每笔固定费用和比例滑点；本地模拟另支持比例手续费，若配置比例手续费再导出 QC 会明确拒绝。
