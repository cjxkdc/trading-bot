# 富途香港与 moomoo AU：paper 与实盘接口

两家平台均提供模拟盘与实盘接口，代码和离线测试已完成。2026-10-02 已登录 Futu OpenD，联网核验美股模拟账户的资金、持仓、挂单以及 AAPL 标的信息和行情快照；本次未下单或撤单。moomoo 与实盘连接仍未联网验收。默认仍为模拟盘；实盘使用独立模块、配置和启动参数。

## 如何切换

| 选择 | 富途香港 | moomoo AU |
|---|---|---|
| 模拟券商模块 | `futu_paper` | `moomoo_paper` |
| 实盘券商模块 | `futu_live` | `moomoo_live` |
| 行情检查模块 | `futu_market` | `moomoo_market` |
| 策略运行配置 | `futu_paper_config.json` | `moomoo_paper_config.json` |
| 实盘运行配置 | `futu_live_config.json` | `moomoo_live_config.json` |
| 本地设置目录 | `secrets/futu/` | `secrets/moomoo/` |
| 默认账本目录 | `runs/futu-paper/` | `runs/moomoo-paper/` |
| 实盘设置目录 | `secrets/futu_live/` | `secrets/moomoo_live/` |
| 实盘账本目录 | `runs/futu-live/` | `runs/moomoo-live/` |
| 本项目约定端口 | `127.0.0.1:11111` | `127.0.0.1:11112` |
| 官方 Python SDK | `futu-api` | `moomoo-api` |
| 配置的券商区域 | `FUTUSECURITIES` | `FUTUAU` |

四个配置继承同一个科技股盈利动量策略，复用同一执行框架。底层协议代码在 `opend.py`、`brokers/opend_broker.py`、`data/opend_market.py`；`brokers/opend_paper.py` 保留旧导入名兼容。paper 固定使用 SIMULATE，live 固定使用 REAL；不能在连接 JSON 中随意覆盖环境。旧 paper 账户指纹、订单备注和账本格式保持兼容。

**11112 是我们为避免端口冲突作的约定，不是 moomoo 的出厂默认值。** 同时开两个 OpenD 时，应在 moomoo OpenD 中改为 11112；也可以在本地 JSON 指定实际端口。

## 现在可以做的离线检查

在项目目录运行（Windows 可将 `python` 换成 `.venv\Scripts\python.exe`）：

```text
python -m pip install -r requirements.txt -r requirements-futu.txt -r requirements-moomoo.txt
python bot.py broker-status --config futu_paper_config.json
python bot.py broker-status --config moomoo_paper_config.json
python bot.py market-status --provider futu_market
python bot.py market-status --provider moomoo_market
```

默认不会联网。`configuration_ready_not_authenticated` 只说明离线设置和 SDK 版本通过检查，不代表账户接通。

也可以对实盘配置执行 `broker-status --config futu_live_config.json` 或 `--config moomoo_live_config.json`。未填写实盘账户与登录 ID 时会返回 `not_ready`，不会尝试连接或自动选择第一个账户。

## 之后登录时

1. 从官方渠道安装对应的 Futu OpenD / moomoo OpenD，在客户端自行登录并确认端口。本程序不保存登录密码。
2. 按需将 `configs/futu_connection.example.json` 或 `configs/moomoo_connection.example.json` 复制为对应设置目录下的 `connection.json`。默认设置相符时可先不创建该文件。
3. `account_id` 是 API 返回的模拟账户 ID，`login_user_id` 是 OpenD 登录的平台用户 ID；填写时用 JSON 字符串。空值只允许自动选出唯一一个符合条件的模拟账户，有多个就停止并要求明确选择。它们不是银行账号、实盘账号或昵称。
4. 运行下面的只读检查。检查只显示账户指纹；原始账户 ID 可在本人 OpenD 账户信息/API 账户列表中确认并填入本地文件。

```text
python bot.py broker-status --config futu_paper_config.json --connect
python bot.py market-status --provider futu_market --connect --symbol AAPL --out runs/futu-data-001

python bot.py broker-status --config moomoo_paper_config.json --connect
python bot.py market-status --provider moomoo_market --connect --symbol AAPL --out runs/moomoo-data-001
```

每次行情检查使用新的报告目录，查询单只股票的快照、已完成日线和财报。历史查询可能消耗该平台的历史行情额度。诊断不调用下单、交易解锁或 Tiger 的设备抢占功能。SDK 错误不原样输出，以免账户信息进入终端或报告；排错时对照 OpenD 客户端查看权限与版本。

## 模拟执行与当前限制

模拟券商已实现账户/持仓读取、美股整股 DAY 限价单、订单查询和撤单。仅常规交易时段，拒绝做空、借贷买入、合成 DEMO 股票以及单笔超过 100,000 美元的订单。所有交易请求显式指定已核验的模拟账户；不使用 SDK 默认的真实交易环境或账户索引。

支持有 US 权限的有效 `STOCK` 模拟账户，以及仅有 US 权限的 `STOCK_AND_OPTION` 美股融资融券模拟账户。后者是富途当前的美股股票/期权合并模拟账户；连接后仍只允许现金覆盖的股票/ETF 交易，并额外核验所有持仓和拟交易标的，拒绝期权和其他衍生品。纯期权、期货、停用及类型不明的账户会被拒绝。模拟账户可能返回 `security_firm=N/A`，因此区域是连接配置，不能把它当作澳洲或香港实盘账户归属的认证结果；账户绑定同时检查平台、登录用户和模拟账号。见[官方账户类型说明](https://openapi.futunn.com/futu-api-doc/trade/get-acc-list.html)。

可用现金通常取 USD 现金与 USD 现金购买力的较小值，不使用跨币种总资产或保证金购买力。已核验仅有 US 权限的合并模拟账户可能返回 `usd_net_cash_power=N/A`：此时取 `us_cash - frozen_cash`，最低为零；缺失冻结资金、错误金额、其他账户类型或实盘均不会使用此兼容路径。订单回包没有佣金字段，框架中 `OrderState.fees=0` 表示费用未取得，**不是零手续费**。买单预检仍扣留配置中的估算费用，账户现金以平台结果为准。

订单意图先写入独立 SQLite 账本，再发送。超时或确认丢失会暂停，重启后按订单 ID/备注查询已有订单，不自动重发。撤单请求被接受后仍需确认最终状态；无法查到的旧订单、过大的订单列表或不一致的成交都会阻止继续。每个账户仅使用一个账本目录，不复制或清空账本来重新运行。

行情诊断报告**不能直接用于调仓**：全科技股股票池、历史时点可得的 TTM 营业利润、行业映射和复权口径仍需接入与核验。财报期末日期不等于公布日期。当前数据模块始终标记 `strategy_ready=false`；不会为了跑通而取消盈利过滤。

取得符合 `us_technology_daily_v1` 的当日真实快照后，才可用现有执行入口：

```text
python bot.py broker-paper --config futu_paper_config.json --snapshot <当日真实快照.json>
python bot.py broker-paper --config moomoo_paper_config.json --snapshot <当日真实快照.json>
```

上述为一次执行/对账过程，尚无持续调度器；保持同一账本处理未完成批次。此次未运行这些联网命令。

## 验证与来源

测试覆盖两套固定版本 SDK 的真实方法签名和调用参数、paper/live 隔离、账户绑定、防重复发送、超时恢复、部分成交、撤单确认与行情失败。实盘测试也完全使用模拟通信；通过离线测试不代表已完成实盘验收。CI 同时安装 Tiger/Futu/moomoo SDK，在 Linux 和 Windows 回归。

Futu 本次联网检查确认 `SIMULATE / STOCK_AND_OPTION / US`，读取资金、持仓和挂单成功，模拟账户已绑定到本地 `secrets/futu/connection.json`。AAPL 标的元数据与快照查询成功；未验证模拟下单、撤单或成交，单只股票快照也不等于完整策略行情输入。账户 ID 和本地验证报告均不提交 Git。

官方资料：[接入架构](https://openapi.futunn.com/futu-api-doc/intro/intro.html)、[模拟下单](https://openapi.futunn.com/futu-api-doc/trade/place-order.html)、[资金字段](https://openapi.futunn.com/futu-api-doc/trade/get-funds.html)、[财务报表](https://openapi.futunn.com/futu-api-doc/quote/get-financials-statements.html)。

## 实盘配置与使用

将 `configs/futu_live_connection.example.json` 或 `configs/moomoo_live_connection.example.json` 复制为对应实盘设置目录内的 `connection.json`。必须填入本人真实 `account_id` 和 `login_user_id`，两者均为数字字符串；端口仍对应同一平台的 OpenD。禁止填写交易密码，凭据和账本均被 Git 忽略。

实盘连接要求账户列表明确返回：REAL、有效状态、普通账户角色、US 交易权限，以及配置对应的香港/澳洲券商。可连接现金或保证金账户，但当前代码只支持现金覆盖的买入，不使用借贷购买力。选择 paper 账户、错误券商或无法验证的账户都会停止。

先做只读检查：

```text
python bot.py broker-status --config futu_live_config.json --connect
python bot.py broker-status --config moomoo_live_config.json --connect
```

该检查不会解锁、下单或撤单。正式执行入口是 `broker-live`；普通 `paper` 和 `broker-paper` 命令会拒绝实盘配置。实盘和模拟盘不共用状态目录，也不能复制账本互换环境。

后续完成登录、权限和数据验收，并明确决定发送实盘订单时，才运行：

```text
python bot.py broker-live --config futu_live_config.json --snapshot <当日真实快照.json> --enable-live
python bot.py broker-live --config moomoo_live_config.json --snapshot <当日真实快照.json> --enable-live
```

`--enable-live` 只为这次程序调用开启实盘发送；没有此参数会在读取快照和连接账户之前停止。`initial_cash` 不会改变真实资金，账户余额由券商查询取得。限价、整股、USD 现金、报价时效、仓位上限与订单恢复沿用同一框架。

实盘还会通过 OpenD 标的基本信息核验持仓和拟交易股票，只允许未退市的美股股票/ETF，拒绝期权、其他衍生品或类型无法确认的代码。已持有的股票必须包含在快照的稳定资产 ID 映射内。

**交易解锁**：GUI 版 OpenD 需要在界面中手动解锁，不使用 `--unlock`。命令行版 OpenD 可以给上述实盘命令加 `--unlock`，程序使用不回显输入读取密码，调用解锁接口；密码不写入配置、日志或账本，不为重连保留密码。官方解锁作用于 OpenD，同一个网关的其他连接也可能共享解锁状态；关闭本程序不会自动重新锁定 OpenD。见[官方解锁说明](https://openapi.futunn.com/futu-api-doc/trade/unlock.html)。

平台解锁不等于本程序启用实盘：即使 OpenD 已解锁，也仍须选择 live 配置和 `--enable-live`。实盘账户的查询/下单/撤单方法与 paper 一致，额外提供 `broker.unlock(password)` 供明确启用的命令行 OpenD 连接使用。

当前仍是单次执行/对账接口，尚无连续行情、全天调度器、公司行动同步或完整科技股数据输入。订单费用字段和数据准备限制与上面的模拟接口相同；实盘成交与账本须在联网验收中进一步核对。
