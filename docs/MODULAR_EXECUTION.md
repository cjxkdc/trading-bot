# 模块化交易执行

平台负责组合、校验、调度、订单账本和记录。市场规则、行情来源与券商连接属于模块，不能通过界面中的某个品牌或“模拟盘”字符串决定平台行为。

## 执行上下文

券商控制接口 `execution_context()` 返回以下记录。注册模块必须明确提供，缺失时不给出可执行状态。

```json
{
  "market": "HK",
  "timezone": "Asia/Hong_Kong",
  "currency": "HKD",
  "asset_class": "equity",
  "instrument_rules": {
    "quantity_step": "100",
    "min_quantity": "100",
    "price_tick": "0.05",
    "contract_multiplier": "1"
  }
}
```

这是接口示例，不是港股全市场统一规则。不同标的有不同手数和报价档位，适配器通过 `Quote.rules` 提供逐标的规则，并实现 `instrument_rules(instruments)`（输入为资产 ID 到券商代码的映射，返回资产 ID 到 `InstrumentRules` 的映射）独立确认这些规则；默认实现使用模块声明的统一规则，有不同手数或档位的适配器必须覆盖此方法。导入快照与冻结订单中的规则必须与该券商接口重新核对，不能用数据文件自己的规则作确认依据；可提供小数数量增量。金额和手续费按乘数计算，订单数量必须符合增量与最低数量。当前通用执行模型是现金覆盖、只做多、限价调仓；保证金、卖空、动态期权风险等要实现对应模型与能力，不能伪装成已有功能。

`OrderRequirements` 是数据与订单的语义边界，包含市场、资产类型、币种、订单类型、数量类型和有效期。策略输入 schema 与订单能力分开检查。不同币种的报价不会自动混算；没有明确外汇转换模型时拒绝执行。

`MarketSnapshot.execution_context` 与逐标的规则进入签名和冻结订单。改变账户环境、时区、币种、数量规则、数据、策略或绑定后，原预览不能用于新的下单。`execution_preparation_v2` 同时保存环境、上下文和原始委托，恢复沿用原数量与限价。

## 市场日历

`trading_sessions(start, end)` 由模块返回实际交易区间，不由前端猜测：

```json
[{"date":"2026-10-08","intervals":[
  {"open":"2026-10-08T09:30:00+08:00","close":"2026-10-08T12:00:00+08:00"},
  {"open":"2026-10-08T13:00:00+08:00","close":"2026-10-08T16:00:00+08:00"}
]}]
```

支持午休、提前收市和连续交易市场。计划使用 IANA 时区与实际区间，星期可选周一至周日。跨午夜区间必须明确业务日期。前端没有可靠交易时段数据时显示待确认。旧的仅日期美股日历保留兼容解释，新市场须提供区间。

## 账户环境与授权

`enabled` 表示模块可配置、查看。`execution_enabled` 表示当前绑定账户获得执行资格。它们不是同一个开关；停用新下单后仍可核对已有订单。

支持 `paper` 本地回放、`broker_paper` 券商模拟、`live` 实盘。OpenD 模拟与实盘分别使用 `OpenDPaperControl` 与 `OpenDLiveControl`，实际返回账户必须匹配模块声明。每个账户独立保存绑定、订单账本和估值历史。

服务器授权文件 `configs/execution_policy.json` 示例结构：

```json
{"version":1,"live_accounts":{"my_live_profile":{"account_ref":"<绑定账户的64位指纹>"}}}
```

指纹由券商控制接口 `execution_account_reference()` 给出，必须覆盖平台、实际交易环境与账户身份。不要把账户密码或 token 写进这个文件。文件不存在或未包含此账户时没有实盘下单权限。网页请求不能创建授权；账户重绑后旧授权失效。CLI 实盘入口也受同一授权控制，每次新委托再次检查。此文件被 Git、发布包和测试副本排除，服务器升级保留原文件。示例文件为空授权，不会启用账户。

## 新模块接入

1. 策略声明所需输入 schema、市场、字段和历史长度；只产生目标，不导入券商 SDK。
2. 数据模块提供匹配 schema、`OrderRequirements`、`execution_signal(raw, clock)` 与不可变快照。`paper_signal` 只作为旧接口别名。
3. 券商模块声明执行上下文、订单能力、认证方式和控制接口。实现账户观测、逐标的报价、实际日历、会话以及只读订单核对；需要实盘时实现可验证的账户身份。
4. 注册 manifest 与账户 profile。Portal 从能力描述生成可用操作，不新增品牌分支。
5. 先用模块契约测试验证数据兼容、错误币种、交易单位、休市、重复下单及恢复，再对具体账户做联网验收。

共享账户中的策略可以独立运行，但一个净额批次必须使用一致的市场、币种和订单语义；框架保留停止实例及外部持仓归属。跨市场、多币种净额组合需要显式转换模型，不会自动换汇。

## 已验证与边界

测试替身覆盖港币整手账户从准备到成交、UTC 小数数量实盘接口、乘数、授权撤销、跨环境阻断、恢复不重发和多时区收益 K 线。这些测试不连接真实券商，不代表已完成相应券商港股或数字资产接入。

现有 OpenD/Tiger 适配器继续声明自己实际支持的订单范围；现有 MOM8、科技股策略及 QC 导出仍使用各自的美股契约。更换模块扩展平台范围，不会自动改写这些策略的金融含义。QC 导出拒绝该适配器尚未实现的市场或时区。旧快照、模拟订单与历史 USD 估值通过明确兼容分支读取，不转换到实盘，不删改原账本。

## 港股接入：设计与验收清单

现状：三个券商适配器（Futu/moomoo OpenD、Tiger 模拟盘、本地回放）都只声明 `market: US`、`currency: USD`、整股、DAY 限价；OpenD 的账户、持仓、订单查询固定在 `TrdMarket.US`，交易日历固定查 `TradeDateMarket.US`，账户观测要求持仓只含 `US.` 代码；两个策略契约（`us_technology_daily_v1`、`us_equity_momentum_daily_v1`）和 Massive 数据源只提供美股；QC 导出拒绝非美股市场。框架层（`framework/market.py` 的 `ExecutionContext` / `InstrumentRules`、`framework/strategy_book.py` 的整手约束、多时区收益 K 线）已经按「市场由模块声明」设计，并有港币整手账户从准备到成交的测试替身。所以港股不是改几个标签的事，而是一套新的数据契约 + 券商市场分支 + 真实账户验收。没有可登录的 OpenD 港股账户，任何「已支持港股」的声明都无法验证，本次只给出设计与验收清单。

**需要新增或改动的模块**

1. 数据契约与数据源：新增 `hk_equity_daily_v1` 输入 schema（`asset_id` 用稳定标识，代码用五位数字如 `00700`，复权日线、成交额、行业；基本面字段按策略需求声明），对应的采集器与快照校验（`data/controls.py` 的 `OrderRequirements('HK', 'equity', 'HKD', 'limit', 'board_lot', 'day')`）。Massive 没有港股，要用 OpenD 行情（`data/futu_technology.py` 已有 `CachedQuoteClient` 的模式可复用）或其他供应商，并补齐批量日线、拆股复权与缓存。
2. 策略：现有策略声明 `market='us'`；港股策略要么是新策略文件（声明 `market='hk'`、港股 schema），要么现有策略增加市场参数并明确行业分类口径（港交所行业与 SIC 不同）。策略只产生目标权重，不碰整手。
3. 券商（OpenD）：`brokers/opend_broker.py` 的 `_scope`/`_account`/`_orders`/下单改为按 profile 的 `market` 选择 `TrdMarket.HK`、`Currency.HKD`、`HK.` 代码前缀；持仓与现金只认 HKD；`brokers/opend_observation.py` 的日历改查 `TradeDateMarket.HK`（半日市 `MORNING` 收盘 12:00，与美股 13:00 不同）；每只股票的整手（`lot_size`）与价位表（港交所按价格区间分档的 `price_tick`）必须从 OpenD 实时取得并写进 `InstrumentRules`（`quantity_step=lot_size`、`min_quantity=lot_size`、`price_tick` 按档位），`BrokerControl.instrument_rules()` 已预留逐标的覆盖。新建 `brokers/plugins/futu_paper_hk.json`（`execution_context`: HK / Asia/Hong_Kong / HKD / equity，`order_capabilities.quantity_type=["board_lot"]`），不改现有美股 manifest。
4. 执行策略：`framework/trading.py` 的订单规划已经通过 `InstrumentRules` 向下取整到交易单位；要确认碎股（不足一手）不会被规划为订单、卖出时碎股只能整手卖、手续费模型（港股印花税 0.1% 双边、交易征费、结算费等按比例+最低收费）在 `Fees` 中可表达，否则新增港股费用模型。
5. 账户表现：`framework/performance.py` 要求观测币种一致，港币账户单独一个 scope；基准改为港股指数或 ETF（如 2800）；收益 K 线时区 Asia/Hong_Kong。
6. 运行实例：同一账户不能混合美股与港股实例（净额批次要求同一市场与币种，`docs/MODULAR_EXECUTION.md` 已写明）；界面在创建实例时按券商 manifest 的市场过滤策略。
7. 回测：QC 导出要么增加港股适配器（QC 有港股数据但口径不同），要么只允许本地回放；本地回放的费用、整手、滑点口径也要按港股改。

**验收清单（需要真实 OpenD 港股模拟账户）**

- [ ] 连接检查返回港币现金与 `HK.` 持仓，美股持仓存在时拒绝执行并给出原因。
- [ ] 交易日历：对比港交所官方日历一个季度（含半日市、台风/黑雨临时休市）；半日市收盘时间正确。
- [ ] 整手与价位：对 5 只不同价位区间的股票读取 `lot_size` 与价位档，规划的订单数量是整手的整数倍、限价在价位档上；碎股持仓不被卖出也不阻塞核对。
- [ ] 费用：对一笔成交核对券商对账单（佣金、平台费、印花税、交易征费、结算费）与 `Fees` 估算的差异在可接受范围内。
- [ ] 一轮完整模拟调仓：准备 → 提交 → 部分成交 → 次日核对 → 关闭终态，所有状态映射与美股一致。
- [ ] 账户表现：港币 scope 的净值曲线与基准收益按香港交易日对齐。
- [ ] 本地回放：港股快照序列至少 2 份，回放结果的费用与整手口径与运行一致。
- [ ] 文档：`platforms/opend.md` 增加港股账户配置与权限（`trdmarket_auth` 含 HK）说明。

完成以上项目前，Portal 的市场选项仍只列出各模块实际声明的市场，不会出现「港股」。
