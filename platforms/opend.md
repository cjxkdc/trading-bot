# 富途香港与 moomoo AU：模拟账户接入

代码和离线测试已完成，尚未登录 OpenD 验证本人的账户、行情权限或模拟订单。此版本只接受 `SIMULATE`，不提供实盘入口。

## 如何切换

| 选择 | 富途香港 | moomoo AU |
|---|---|---|
| 模拟券商模块 | `futu_paper` | `moomoo_paper` |
| 行情检查模块 | `futu_market` | `moomoo_market` |
| 策略运行配置 | `futu_paper_config.json` | `moomoo_paper_config.json` |
| 本地设置目录 | `secrets/futu/` | `secrets/moomoo/` |
| 默认账本目录 | `runs/futu-paper/` | `runs/moomoo-paper/` |
| 本项目约定端口 | `127.0.0.1:11111` | `127.0.0.1:11112` |
| 官方 Python SDK | `futu-api` | `moomoo-api` |
| 配置的券商区域 | `FUTUSECURITIES` | `FUTUAU` |

两个配置继承同一个科技股盈利动量策略；切换配置不用改策略或执行框架。底层协议代码在 `opend.py`、`brokers/opend_paper.py`、`data/opend_market.py`，各平台保留独立入口和设置。

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

当前只支持 `sim_acc_type=STOCK` 且有 US 权限的有效模拟账户。股票与期权混合账户、竞赛账户等会被拒绝。模拟账户可能返回 `security_firm=N/A`，因此区域是连接配置，不能把它当作澳洲或香港实盘账户归属的认证结果；账户绑定同时检查平台、登录用户和模拟账号。

可用现金取 USD 现金与 USD 现金购买力的较小值，不使用跨币种总资产或保证金购买力；缺少对应字段会停止。订单回包没有佣金字段，框架中 `OrderState.fees=0` 表示费用未取得，**不是零手续费**。买单预检仍扣留配置中的估算费用，账户现金以平台结果为准。

订单意图先写入独立 SQLite 账本，再发送。超时或确认丢失会暂停，重启后按订单 ID/备注查询已有订单，不自动重发。撤单请求被接受后仍需确认最终状态；无法查到的旧订单、过大的订单列表或不一致的成交都会阻止继续。每个账户仅使用一个账本目录，不复制或清空账本来重新运行。

行情诊断报告**不能直接用于调仓**：全科技股股票池、历史时点可得的 TTM 营业利润、行业映射和复权口径仍需接入与核验。财报期末日期不等于公布日期。当前数据模块始终标记 `strategy_ready=false`；不会为了跑通而取消盈利过滤。

取得符合 `us_technology_daily_v1` 的当日真实快照后，才可用现有执行入口：

```text
python bot.py broker-paper --config futu_paper_config.json --snapshot <当日真实快照.json>
python bot.py broker-paper --config moomoo_paper_config.json --snapshot <当日真实快照.json>
```

上述为一次执行/对账过程，尚无持续调度器；保持同一账本处理未完成批次。此次未运行这些联网命令。

## 验证与来源

测试覆盖两套固定版本 SDK 的真实方法签名和调用参数、实盘拦截、账户绑定、防重复发送、超时恢复、部分成交、撤单确认与行情失败。网络通信全部使用测试替身；联网验收留到本人登录之后。CI 同时安装 Tiger/Futu/moomoo SDK，在 Linux 和 Windows 回归。

官方资料：[接入架构](https://openapi.futunn.com/futu-api-doc/intro/intro.html)、[模拟下单](https://openapi.futunn.com/futu-api-doc/trade/place-order.html)、[资金字段](https://openapi.futunn.com/futu-api-doc/trade/get-funds.html)、[财务报表](https://openapi.futunn.com/futu-api-doc/quote/get-financials-statements.html)。
