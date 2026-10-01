# 策略模块

这里是可插拔的交易规则。默认策略为 `tech_profit_momentum`：科技股、过去 12 个月营业利润为正、最近 20 日平均成交额至少 1,000 万美元，按 252 日跳过最近 21 日的动量排名；最多 5 只、每只目标 19%、同一细分行业最多 2 只。

当前没有正动量门槛、止损或市场趋势开关。95% 是目标总仓位上限；5% 是执行缓冲，未证明这个比例能提高收益。

## 组合策略的统一接口

```python
from framework.portfolio import HistoryRequirements, StockTarget

class MyStrategy:
    @property
    def history_requirements(self):
        return HistoryRequirements(price_bars=253, dollar_volume_bars=20)

    def select(self, stocks):
        # stocks 只含当时已知的标准数据。
        # 返回 StockTarget(asset_id, weight, score) 的 tuple。
        # 空 tuple 代表目标现金；数据故障必须由数据层报错，不能冒充空池。
        return ()
```

`select()` 是确定性计算：同一输入和参数输出同一组合，不读网络、不下单、不修改输入、不依赖当前电脑时间。框架可能重算同一决策用于核验；需要持久化内部状态的策略需另行扩展接口。

## 插入一个策略

1. 在这里新增 Python 文件，实现上述接口，构造参数建议提供默认值。
2. 在 `plugins/` 新建 JSON 登记，参考 `tech_profit_momentum.json`。
3. 在自己的配置里选择 `strategy.type` 和参数。只在共享同一输入格式的策略间直接替换。
4. 用 `bot.py plan` 预览；用 `bot.py export-qc` 生成新回测实验。

配置使用 `extends` 时，`strategy` 整块替换，避免上一策略的参数混入新策略。框架不导入具体策略名称；登记文件会加载受信任的本地代码。

`ma`、`momentum`、`hold` 是早期单标的接口，供 `research.py` 使用；它们不能直接插入多股票组合入口。`equal_weight` 按稳定 ID 选股，只用于验证接口切换，不是投资基准。

## 当前可比较的三个研究版本

| 配置 | 动量 | 盈利筛选 | 同行业上限 |
|---|---|---|---|
| `configs/momentum_only.json` | 相同 | 无 | 5 只（不额外约束） |
| `configs/momentum_profit.json` | 相同 | 有 | 5 只 |
| `portfolio_config.json` | 相同 | 有 | 2 只 |

这三项实验用于检查盈利筛选、行业限制各自的贡献；没有因本次改造选出新的收益冠军。股票池、时间段、成本和资金必须一致。记录所有试验，并另行确定未参与调参的数据或未来模拟观察期。
