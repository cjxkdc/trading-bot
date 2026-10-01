# Tiger 只读连接

`tiger_readonly` 是独立的 `account_reader` 插件，未实现交易券商接口。保留 `portfolio_config.json` 中的 `broker.type=paper`；不要改成这个只读插件来运行 paper。

## 准备与检查

1. 安装可选依赖：`python -m pip install -r requirements-tiger.txt`。
2. 在 Tiger OpenAPI 后台申请权限，使用官方导出的签名认证配置。
3. 本地建立 `secrets/tiger/`，把 `tiger_openapi_config.properties` 放进去。它包含私钥，不上传 GitHub、不粘贴进聊天。

```text
python bot.py broker-status --config-dir secrets/tiger
```

默认只检查 SDK 版本、文件、必要字段及配置冲突，不构造 SDK 客户端或发送网络请求。`configuration_present_not_authenticated` 表示配置存在，不代表私钥有效或账户已连接。未就绪时退出码为 2。

明确要查询自己账户时运行：

```text
python bot.py broker-status --config-dir secrets/tiger --connect
```

只发送账户、资产、持仓和未完成订单查询；账户编号显示末四位，响应只输出允许的字段。输出包含真实资金和持仓信息，如需保存请放 `runs/`。买入力度字段是券商返回的购买力，不能当作现金使用。

## 限制

- 对接并核验的 SDK 版本为 `tigeropen==3.8.0`。支持导出的 Tiger ID/私钥签名认证，尚不支持 OAuth2。
- SDK 会优先读取 `TIGEROPEN_*` 环境变量；这个适配器要求先清除覆盖值，避免查询到另一个账户。不要把密钥放在命令行参数里。
- 真实账户和 Tiger 模拟账户均按官方配置使用 PROD 网关，账户本身决定类型；SDK 的旧 SANDBOX 开关不用于这里。
- 使用 SDK 默认官方公共网关，不接受自定义地址；关闭动态域名请求和自动 token 刷新。部分账户若需要其他授权/区域配置，应按官方说明继续核对。
- 查询是依次进行的，不是原子快照。缺失响应报错；未完成订单的分页完整性未作为执行依据核验。这个输出不能直接驱动真实交易。
- SDK 请求边界只允许四个查询方法；下单、改单、撤单、转账均在本地拦截。不会启动交易程序、定时任务或服务器。
- 已有离线 SDK 对象、请求白名单和错误脱敏测试；没有使用真实凭证验证网络连接。行情权限、完整科技股股票池与历史基本面不包含在此次账户接入中。

已有的 `paper.py` 是独立本地模拟账户；不等于 Tiger 模拟盘。下一步需要用户自己的配置文件完成只读验连，再开发和验证券商模拟盘的订单生命周期。

官方依据：[接入准备](https://docs.itigerup.com/docs/prepare)、[账户接口](https://docs.itigerup.com/docs/accounts)、[Python SDK](https://github.com/tigerfintech/openapi-python-sdk)。
