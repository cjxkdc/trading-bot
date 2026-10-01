# 回测放在哪里

回测属于项目的研究运行方式，与实盘共同使用 `strategies/` 中的选股代码。

| 代码 | 职责 |
|---|---|
| `framework/backtest.py` | 已有的单标的 CSV 研究模型、成本和收益统计 |
| `research.py` | 单标的候选筛选、冻结、验证和最终测试 |
| `platforms/quantconnect/adapter.py` | 多股票历史数据、交易日历和 LEAN 回测接入 |
| `tools/build_quantconnect.py` | 将配置、策略和框架合成可复制的 QC 文件 |
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

在对应实验目录保存 QC 报告、订单/交易导出、日志、云端回测 ID 和运行时 Version。检查起止日期、成本、是否完整执行、收益、最大回撤、QQQ 对照，以及利润是否集中在极少数股票上。

本次 v0.3 检查验证了模块边界、日期控制、下单保护和同输入决策一致性；尚未运行新的云端历史回测。旧版本报告不能归到新版名下。

## 回测和本地模拟的区别

QuantConnect/LEAN 负责历史撮合、分红和拆股。本地 `paper` 只是对文件中的报价立即模拟成交，不含连续历史、结算、公司行动或盘口，因此不能用 demo 的账户变化声称历史收益。

两者共用选股规则、参数和目标仓位上限；交易时点、费用处理、整股撮合、执行限制仍需分别核验。当前 QC 导出只支持每笔固定费用和比例滑点；本地模拟另支持比例手续费，若配置比例手续费再导出 QC 会明确拒绝。
