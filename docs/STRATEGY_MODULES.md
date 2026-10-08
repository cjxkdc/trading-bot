# 策略模块开发

策略目录的用户说明见 [策略目录](../strategies/README.md)。本文记录模块接口、登记和数据约定。

## 组合接口

```python
from framework.portfolio import HistoryRequirements, StockTarget

class MyStrategy:
    @property
    def history_requirements(self):
        return HistoryRequirements(price_bars=253, dollar_volume_bars=20)

    def select(self, stocks):
        return ()
```

`select()` 接收当时已知的标准化数据，返回 `StockTarget(asset_id, weight, score)` 元组。空元组表示目标现金；数据故障由数据层报告，不能伪装成空股票池。

同一输入和参数必须产生相同结果。策略不读网络、不下单、不修改输入、不依赖当前电脑时间；框架会重算决策用于核验。

## 持仓输入

需要持仓信息的策略声明 `requires_context = True`，实现 `select_with_context(stocks, context)`。不可变的 `PortfolioContext` 包含决策日期 `as_of` 和持仓标识 `held_asset_ids`，不包含券商对象或凭据。

```json
"portfolio_context": {"as_of": "2026-09-01", "held_asset_ids": []}
```

空名单表示已确认无持仓，缺失字段不等于空仓。首次执行核对实际账户；同批次恢复使用原始冻结输入。未来日期、早于信号的持仓状态或不匹配的账户输入会被拒绝。

MOM8 使用该接口实现排名缓冲。名单不含股数和估值，不能用于保留原持仓金额而不配平。

## 登记与策略说明

在 `strategies/plugins/` 新建 JSON，声明 `type`、`kind`、`version`、`entrypoint`、数据契约与参数元信息。`type` 是稳定标识，已有实例和配置依靠它引用策略；展示名称和说明可独立维护。

可选 `readme` 路径相对于 `strategies/`，例如 `MOM8.md` 或 `my_strategy/README.md`。UTF-8 Markdown 文件最大为 256 KB；缺失文件只影响说明展示，不会停用策略。原始 HTML 不执行，远程图片不自动加载。

策略页文档只说明策略思路、交易规则、参数、数据与特征。安装流程、运行操作、接口设计和开发历史应放在对应开发或使用文档中。

## 接口类型

| 类型 | 入口 | 输出 |
|---|---|---|
| `portfolio` | `select()` 或 `select_with_context()` | 多标的目标权重 |
| `single` | `target(history)` | 单标的 0–1 目标信号 |

单标的接口由 `app/research.py` 和 `backtests/engine.py` 使用，不直接接入组合执行入口。单标的信号再乘以研究配置中的资金比例。

新增策略后，选择匹配的数据契约进行预览和回测。使用 `extends` 的配置整体替换 `strategy` 块，避免混入另一策略的参数。组合策略可通过 `bot.py export-qc` 导出 QuantConnect 回测；回测与运行计划需使用一致的调仓规则。

## 数据与许可

价格复权、行业分类和基本面可用时间由数据契约与供应商实现明确。研究快照不自动具备历史时点有效性，也不代替券商执行报价。

第三方代码来源、版权与许可集中记录于 [第三方声明](THIRD_PARTY_NOTICES.md)，分发时须保留。
