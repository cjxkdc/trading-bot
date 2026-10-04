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

## 需要知道已经持有什么时

策略可声明 `requires_context = True`，改用 `select_with_context(stocks, context)`；无状态策略继续使用原来的 `select(stocks)`。`PortfolioContext` 只有 `as_of` 日期和不可变的 `held_asset_ids`，不包含账户密码或 SDK 对象。

这能支持“已持有股票稍微跌出前五时先保留”的排名缓冲规则。当前默认策略不使用这个接口，未加入排名缓冲；名单不包含股数或估值，所以也不能据此实现“不补仓”的金额计算。

QC 从模拟持仓中冻结名单；本地快照可增加：

```json
"portfolio_context": {"as_of": "2026-09-01", "held_asset_ids": []}
```

空名单表示确实没有持仓，缺失字段不等于空账户。需要 context 的策略会拒绝缺失输入。首次执行会核对名单与本地账户实际持仓；重复执行同一批次使用同一份原始快照，即使成交后持仓已经改变。未来持仓日期、早于信号的持仓状态和不同账户名单会被拒绝。

## 可选的策略说明

在策略登记 JSON 中增加可选的 `readme` 字段，路径相对于 `strategies/`：

```json
"readme": "MOM8.md"
```

也可以使用 `my_strategy/README.md`，将说明和实现放在同一子目录。文件使用 UTF-8 Markdown，最大 256 KB。工作台会在该策略卡内显示可展开的「策略说明」，支持标题、列表、表格和代码块；没有登记说明时不显示该区域。文件缺失或说明格式有误只影响说明展示，不会停用策略。

文档随本地包与服务器包一起分发，具体文件名由每个策略自己登记。原始 HTML 不执行，图片不自动加载；HTTP/HTTPS 参考链接在新标签页打开，仓库相对链接保留文字。说明属于文档，不参与选股或下单。

## 插入一个策略

1. 在这里新增 Python 文件，实现上述接口，构造参数建议提供默认值。
2. 在 `plugins/` 新建 JSON 登记，参考 `tech_profit_momentum.json`。
3. 在自己的配置里选择 `strategy.type` 和参数。只在共享同一输入格式的策略间直接替换。
4. 用 `bot.py plan` 预览；用 `bot.py export-qc` 生成新回测实验。

配置使用 `extends` 时，`strategy` 整块替换，避免上一策略的参数混入新策略。框架不导入具体策略名称；登记文件会加载受信任的本地代码。

`ma`、`momentum`、`hold` 是早期单标的接口，供 `app/research.py` 使用；它们不能直接插入多股票组合入口。`equal_weight` 按稳定 ID 选股，只用于验证接口切换，不是投资基准。

## 当前可比较的三个研究版本

| 配置 | 动量 | 盈利筛选 | 同行业上限 |
|---|---|---|---|
| `configs/momentum_only.json` | 相同 | 无 | 5 只（不额外约束） |
| `configs/momentum_profit.json` | 相同 | 有 | 5 只 |
| `configs/portfolio_config.json` | 相同 | 有 | 2 只 |

这三项实验用于检查盈利筛选、行业限制各自的贡献；没有因本次改造选出新的收益冠军。股票池、时间段、成本和资金必须一致。记录所有试验，并另行确定未参与调参的数据或未来模拟观察期。
