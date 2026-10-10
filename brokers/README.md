# 券商模块

| 插件 | 用途 | 资金与撮合 |
|---|---|---|
| `paper` | 本地文件报价回放 | 程序本地创建虚拟资金，立即模拟全额成交 |
| `tiger_paper` | Tiger 官方模拟账户 | Tiger 返回虚拟资金、持仓及模拟订单状态 |
| `futu_paper` | 富途 OpenD 模拟账户 | 已联网核验账户资金、持仓和挂单；兼容美股合并模拟账户，下单/撤单仍待验收 |
| `moomoo_paper` | moomoo OpenD 模拟账户 | moomoo 返回模拟资金和订单；代码已离线验证，尚未登录验收 |
| `futu_live` | 富途香港实盘接口 | REAL 账户；独立配置/账本，显式启用才可发送真实订单；尚未联网验收 |
| `moomoo_live` | moomoo AU 实盘接口 | REAL 账户；独立配置/账本，显式启用才可发送真实订单；尚未联网验收 |
| `tiger_readonly` | 显式选择账户的只读检查 | 只能查询；不属于可下单的 broker |

当前券商操作默认选择 `tiger_paper`，使用 `configs/tiger_paper_config.json`。该文件继承主策略，只替换 broker 块；更换策略不用修改 Tiger SDK 代码。

Futu 香港与 moomoo AU 的独立配置、端口和后续登录步骤见 [OpenD 接入说明](../platforms/opend.md)。下文是 Tiger 专用说明。

## 配置与查询

使用 Python 3.11+，安装 `requirements/tiger.txt`（SDK 固定为 `tigeropen==3.8.0`）。在 Tiger 开发者中心选择“模拟账户”，导出配置并保存到：

```text
secrets/tiger_paper/tiger_openapi_config.properties
```

凭据路径相对项目根目录；可以显式传绝对路径。私钥、账本、运行报告都被 Git 忽略。模拟与实盘均用 PROD 网关，模拟账户不是 SDK 的旧 SANDBOX 环境；账号类型决定虚拟资金账户。

```text
python bot.py broker-status
python bot.py broker-status --connect
```

第一个命令只检查配置，不联网。第二个命令验证服务端 `account_type=PAPER`，查询 USD 可用现金、持仓及挂单数。`configuration_present_not_authenticated` 只表示配置完整；`connected` 才表示接口验证成功。不会因为查询而下单。

模块在构造 SDK 前拒绝实盘账号；服务端确认 PAPER 后才允许资产和订单请求；请求边界要求每次调用都属于同一个模拟账号。SDK 日志关闭，异常文本脱敏，不在命令行或结果中输出私钥。暂不支持 OAuth2、自定义网关或 `TIGEROPEN_*` 环境覆盖。

## 执行一轮模拟调仓

```text
python bot.py plan --config configs/tiger_paper_config.json --snapshot runs/current-market.json
python bot.py broker-paper --snapshot runs/current-market.json
```

快照需要当时可知的股票池、历史基本面与当日真实买卖报价。`demo` 的合成数据不可用于 Tiger。策略和框架继续使用标准输入，broker 不负责产生选股数据。当前未自动获取这些实时输入，也没有启动定时任务。

平台从快照的 `asset_id` 与 `ticker` 建立映射，把框架的稳定股票编号转换成 Tiger 代码；持仓再反向转换。快照必须包含全部已持仓股票的映射。每个订单单独保存提交时的 ticker，恢复时不依赖重新推断标的。

固定使用配置指定的 `runs/tiger-paper/` 状态目录，保存账户绑定、订单意图、Tiger 订单号及框架 journal。不要复制、清空状态目录或用新目录重试同一账户。初始化只创建本地记录，绝不重置 Tiger 的虚拟资金；`initial_cash` 只供本地模拟和历史回测使用。

支持 USD 股票、整股、仅做多、DAY 限价单、正常交易时段。使用 USD 现金和可交易现金的较小值，不把港币折算金额或保证金购买力当现金。维持单笔 100,000 美元上限；较大的虚拟账户调仓可能触发上限并暂停，不会自动加大限额或拆单。

未成交或部分成交会让批次暂停；再次运行同一决策时先查询原订单，不再提交一份。网络超时后若找不到对应订单，状态仍为 unknown，不能当作提交失败重发。历史订单逐页读取；分页不完整或未知状态会阻断执行。

订单查询和撤销使用 broker 标准方法 `query(client_id)`、`cancel(broker_id)`。撤销只能针对本地账本记录的订单，撤单应答后继续查询最终状态；拒单、撤单或未解决订单会阻止后续组合批次，需要人工核对和处置。当前没有自动放弃旧批次或跨日修复流程。

Portal 的「核对订单状态」「结束未提交余单」「关闭已撤单/拒单」「确认未发送并关闭」对 Tiger 模拟盘同样可用：`TigerPaperControl.reconciliation_session` 以只读会话打开账本（`read_only=True` 的 broker 拒绝 `submit`/`cancel`），只查询订单与账户；「确认未发送并关闭」要求账本记录了委托的创建时间（`created_at`，2026-10-11 起写入，旧账本自动补列但旧记录不能作证明），并在之后的交易日按创建日、次日和当天分别查询带 `user_mark` 标记的订单列表，三者都没有才视为从未送达。

## 已做的验证

2026-10-02：Tiger 返回 PAPER 类型，USD 虚拟可用现金 1,000,000；连接测试提交了 1 股 AAPL、限价 1 美元的模拟单，随后撤单成功，成交 0、剩余挂单 0。结果保存在本地 `runs/tiger-paper/connection-smoke.json`。这是接口与订单生命周期验证，不是策略回测或连续模拟交易结果。

离线检查覆盖：实盘拦截、账户隔离、SDK 状态映射、持仓与现金限制、部分成交、断线后按订单标记恢复、不重复发送、撤单应答、分页和过期行情。

## 旧只读入口

需要检查显式指定的其他配置时：

```text
python bot.py broker-status --broker tiger_readonly --config-dir <配置目录>
```

仍然只有显式增加 `--connect` 才联网；该模块不能作为执行 broker。当前默认配置不使用实盘凭据。

官方依据：[接入准备](https://docs.itigerup.com/docs/prepare)、[账户接口](https://docs.itigerup.com/docs/accounts)、[订单查询](https://docs.itigerup.com/docs/orderinfo)、[下单接口](https://docs.itigerup.com/docs/place-order)、[Python SDK](https://github.com/tigerfintech/openapi-python-sdk)。

## Portal 账户与连接配置

在「连接 → 券商账户」中选择账户模块、账户环境及交易市场。环境和市场选项只来自同一 `configuration_family` 下已登记的账户配置；币种和时区由选中模块提供，不能单独改成与订单不一致的值。当前 Futu/moomoo OpenD 适配器实际实现 US 股票，其他市场需安装对应适配器，不会仅修改界面标签就启用交易。

Futu/moomoo 的模拟和实盘连接均可在页面保存 API 端口、账户 ID 和 OpenD 登录用户 ID。OpenD 地址限于运行工作台机器的 `127.0.0.1`，云端连接云端的 OpenD。敏感 ID 不回传页面，留空保留已有值；模拟和实盘配置独立，选择实盘不会自动授予下单权限。

保存前需暂停自动计划与策略实例、核对未完成委托。已有账本的账户不可直接改绑其他账户。保存会清除旧连接核验与订单预览，不发送订单、不调用券商登录。多个页面同时编辑时，旧版本的保存会被拒绝。

模块提供 `configure` 能力、`settings_fields`、`public_settings()` 与 `configure(values)` 即可接入表单；`public_settings()` 返回不含凭证的 `values` 和已配置字段名称 `configured_fields`。接口为 `POST /api/broker/settings`，提交所选 broker、读取时的 revision 和 values。新增券商无需在 Portal 组件里添加券商分支。
