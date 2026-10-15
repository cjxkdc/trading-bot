# AWS 上运行 Trading Bot

## 当前支持范围

同一仓库维护桌面版和服务器版。Ubuntu 包包含已构建的 Portal，无需服务器安装 Node，也不需要 Windows 桌面。后台在线、自动调仓启用、OpenD 已登录是不同状态：部署与升级本身不代表自动交易已经启用。自动调度的周期、开关和执行状态以 Portal 的自动运行设置为准；手动单次模拟交易仍然保留。

现有 moomoo/Futu OpenD 模拟盘支持准备与执行订单；Linux 上需要另装官方 OpenD 并重新登录、重新绑定模拟账户。新增 moomoo 网页沙盒授权仅用于读取账户；它不能下单，也不能替代原有 OpenD 模拟盘。Tiger 仍使用其现有 API 配置。实盘模块在 Portal 中保持禁用。

## 自动运行与后台在线

“后台运行中”表示服务在线；“自动运行已启用”才表示调度器按规则工作。自动运行默认关闭，需要在 Portal 单独开启，并核验当前模拟账户。关闭浏览器不会暂停已启用的云端自动运行；暂停时使用 Portal 自动运行开关。

默认计划以 **America/New_York（纽约时间，随夏令时调整）** 为准：

| 阶段 | 默认时间与条件 |
| --- | --- |
| 数据准备 | 每个交易日 06:00 更新策略所需数据；失败最多重试 3 次，间隔 15 分钟 |
| 月度调仓 | 当月第一个交易日 09:35 开始，允许提交订单的窗口为 1 小时 |
| 订单跟进 | 核对已有订单成交状态，卖出确认后再推进买入；传输结果不确定时暂停 |
| 日终核对 | 每个交易日 16:20 只读核对账户、挂单与本地账本 |

首次开启会立即检查账户、更新数据并选股，首轮建仓在当前或下一可用交易时段开始。后续调仓可选每日、每周指定工作日或每月 1–28 日起的首个交易日。调仓时间和窗口可配置，实际交易日使用券商模块提供的交易日历。常规周期错过窗口不会追单；执行进度保存到服务器状态中，服务重启不能让同一周期重新下单。

存在未明确归属的订单、账户或模块变化、策略配置/代码变化时，自动运行暂停，需核对后重新开启。异常结果不会自动撤单或切换真实账户。连接、数据、报价和策略条件尚未满足时，Portal 应显示等待或阻止原因；运行日志不能只用“服务在线”代替实际执行状态。

## 创建实例

AWS Lightsail → Create instance → Linux/Unix → **OS Only / Ubuntu 24.04 LTS** → x86_64 → 2 vCPU / 4 GB 内存 / 80 GB 磁盘。有公网 IPv4 的这档官方标价 24 USD/月，税费、快照和超额流量另计；创建前以控制台价格为准。先选靠近你所在地的区域。

为实例附加 Static IP。网站开放 TCP 80 和 443；80 仅用于证书验证及跳转到 HTTPS。管理入口保留 Lightsail 浏览器 SSH，SSH 22 不向所有来源开放。**不要开放 5329 或 OpenD 的 11111/11112**。不要把私钥、账户密码或访问密钥发进聊天。

资料：[AWS 套餐](https://docs.aws.amazon.com/lightsail/latest/userguide/amazon-lightsail-bundles.html)、[系统镜像](https://docs.aws.amazon.com/lightsail/latest/userguide/compare-options-choose-lightsail-instance-image.html)。

## 首次安装

将 `Trading-bot-server.zip` 上传至实例用户目录，解压到例如 `/home/ubuntu/trading-bot`，该目录应直接包含 `app/`、`configs/`、`deploy/`。使用实例普通用户运行：

```bash
cd ~/trading-bot
bash deploy/server/install.sh
```

安装器会安装 Python 基础依赖、提示你设置 **Portal 自己的登录密码**，并建立用户 systemd 服务；密码输入隐藏，仅保存加盐哈希。填写 `https://你的固定IP` 或 HTTPS 域名。服务只监听服务器 `127.0.0.1:5329`，公网入口由下方的 HTTPS 网关提供。

发布包不包含本机 `secrets/`、Massive 密钥、OpenD 登录、账户绑定、缓存、订单账本或策略预览；这些需在服务器重新配置。**已有安装使用下方的校验升级工具，不要重新解压覆盖，更不要删除 runs/secrets。** 首次安装器不会迁移已有账户账本。

## 校验升级与回退

本地重新构建网页后运行 `python tools/package_release.py --output runs/releases`，会生成桌面包、服务器包，以及各自的 `.zip.sha256` 文件。服务器包中的清单覆盖每个公共文件。校验和用于检查完整性，不能证明发布者身份；只使用自己构建或已经核验来源的包与升级工具。

把新的服务器 ZIP 和经过核验的 `deploy/server/upgrade.py` 上传到实例用户目录。`v2026.10.15-pre-release` 发布页单独提供新版 `upgrade.py` 与摘要；旧工具不认识新增的根目录文档，升级该版本时应先取得新工具。初次使用升级工具时单独上传该工具，不能先把整包解压进运行目录。以下 `SHA256` 需替换为本次构建产生的完整摘要：

```bash
python3 ~/upgrade.py --project ~/trading-bot --archive ~/Trading-bot-server.zip --sha256 SHA256
```

默认只检查：拒绝路径越界、重复文件、符号链接、缺失清单项和损坏文件。确认变更列表后，先在 Portal 暂停自动运行、等待当前任务结束，再停止服务并更新：

```bash
systemctl --user stop trading-bot.service
python3 ~/upgrade.py --project ~/trading-bot --archive ~/Trading-bot-server.zip --sha256 SHA256 --apply
```

升级工具也会核实服务已停止。它只更新公共源码与网页，备份到 `runs/upgrades/本次编号/`；**不覆盖 configs、secrets、runs 中的原有状态和 .venv**。新包的配置默认值保存在备份下的 `release-defaults/configs/`，需要逐项审阅后合并；已有账户绑定、调度开关、数据缓存和订单账本继续保留。新包已移除、且仍与旧发布清单一致的公共文件会备份后删除；有本地修改或未被清单管理的文件不会被自动删除。回退会恢复本次删除的文件。

若依赖声明改变，再从项目目录用 `.venv/bin/python -m pip install -r requirements/server.txt` 安装，并按选用券商安装其依赖。新源码安装成功不代表依赖、服务单元或实际交易已经验证；更新服务单元需要重新渲染并执行 `systemctl --user daemon-reload`。完成检查后再启动：

```bash
systemctl --user start trading-bot.service
systemctl --user status trading-bot.service
```

升级过程中写入失败会尝试还原本次已改动的公共文件。若新版本启动后需要回退，停止服务，使用升级输出的确切备份目录：

```bash
python3 ~/upgrade.py --project ~/trading-bot --rollback ~/trading-bot/runs/upgrades/本次编号
python3 ~/upgrade.py --project ~/trading-bot --rollback ~/trading-bot/runs/upgrades/本次编号 --apply
```

回退同样默认预览，校验备份摘要，且发现升级后又编辑过的文件会停止，避免覆盖后来的修改。它不回退依赖版本、配置或交易账本。

## 发布网站与打开云端 Portal

保持现有登录密码，迁移旧安装的访问地址（先暂停自动运行、等待任务结束并停止服务）：

```bash
.venv/bin/python deploy/server/publish_origin.py --project "$PWD" --origin https://YOUR_STATIC_IP --apply
systemctl --user start trading-bot.service
```

在 AWS 防火墙开放 80/443 后，阅读 [Let's Encrypt 订户协议](https://letsencrypt.org/repository/) 并安装网关：

```bash
bash deploy/server/install_web.sh https://YOUR_STATIC_IP --agree-tos
```

安装器使用 Nginx 和独立 Certbot 环境。固定 IP 使用受信任的短期证书，定时器每天检查两次续期，续期成功后重新加载 Nginx。必须保持 80 端口用于验证；80 不提供工作台/API，只验证证书并跳转到 HTTPS。证书文件不进入项目或发布包。

直接在浏览器输入 HTTPS 地址，用现有工作台账号登录。也可在本机 `configs/portal/cloud.json` 设置 `{"url":"https://YOUR_STATIC_IP"}`，双击 `deploy/desktop/Open Cloud Portal.cmd`。配置只有网址，不保存 SSH、账户或登录凭据，也不创建后台连接进程。

`Trading Bot.exe` 打开本机工作台，云端网站控制服务器后台。关闭网页和退出登录都不会停止后台；要停止后台须关闭 Portal 右上角开关。

## Portal 的服务器页面

云端 Portal 提供单独的服务器页面，将机器状态与交易状态分开：服务在线不代表策略已开启，也不代表 OpenD 登录仍然有效。页面读取当前服务器的可用系统指标与服务状态；没有权限或系统不支持的指标显示未知，不填入虚构数值。

“AWS 控制台”和“打开 AWS 终端”在新标签页打开 AWS 自己的页面。AWS 登录由 AWS 处理；Portal 不保存 AWS 密码、访问密钥或浏览器会话，也不把 SSH 私钥上传到网页。终端是 AWS 官方浏览器 SSH，Portal 内没有任意命令输入接口。当前固定操作的可用性以页面显示为准；进行维护前暂停自动运行并等待任务结束，不强制杀死交易工作进程。

服务器重启、关机、删除实例等主机操作仍在 AWS 控制台中进行，不能通过 Trading Bot 的交易接口触发。如果网站不可用，可直接访问 [AWS Lightsail 控制台](https://lightsail.aws.amazon.com/)，选择实例的 Connect → Connect using SSH。AWS 官方也支持实例列表中的快速连接按钮。参见 [AWS 浏览器 SSH 说明](https://docs.aws.amazon.com/lightsail/latest/userguide/lightsail-how-to-connect-to-your-instance-virtual-private-server.html)。

### 配置服务器标签和终端入口

部署信息保存在服务器私有文件 `secrets/portal/deployment.json`，与 Portal 密码文件 `secrets/portal/server.json` 分开。公共发布包只包含 `deploy/server/deployment.example.json` 样例，不写入你的账户编号、服务器 IP 或私有终端链接。

在项目目录运行以下命令，替换区域和实例名：

```bash
.venv/bin/python -m deploy.server.configure_deployment --provider aws_lightsail --region ap-southeast-2 --instance-name YOUR_INSTANCE_NAME
```

这会原子创建权限 600 的配置文件，只写部署信息，保留现有认证和券商设置。已经存在时默认不覆盖；确定要更新部署标签才加 `--replace`。无需提供 AWS API 密钥。

`terminal_url` 是可选项：先在 AWS 控制台真正打开该实例的浏览器 SSH 窗口，将其 HTTPS 地址作为 `--terminal-url 'AWS_TERMINAL_URL'` 参数传给同一命令。工具只接受官方 AWS 控制台域名、匹配当前区域和实例的 SSH 终端路径，以及 `protocol=ssh` 查询参数；不接受令牌、命令、重定向或其他查询参数。AWS 的账户专属终端地址并不是此程序承诺长期稳定的 API；失效时从官方控制台重新打开并更新配置。

未配置终端地址时，不会猜测或生成账户专属终端链接；仍可使用官方控制台入口。部署文件缺失或不合格时，页面显示部署信息未配置，不影响原有策略、券商及数据模块。

## OpenD 模拟盘

官方提供 Ubuntu 命令行 OpenD。下载正确品牌的官方包，保留它的配置文件和账户数据文件。API 地址保持 `127.0.0.1`；本项目默认 Futu 使用 11111，moomoo 使用 11112，均不向公网开放。登录信息只保存在服务器自己的私有目录。按官网步骤完成登录、设备验证，再使用程序现有账户绑定工具重新选取 **SIMULATE 美股账户**。不要把 Windows 的私有目录整包上传，也不要解锁真实账户。

安装对应 SDK：

```bash
cd ~/trading-bot
.venv/bin/python -m pip install -r requirements/moomoo.txt
# 使用 Futu 时改为 requirements/futu.txt
```

首次 OpenD 登录和账号绑定仍需初始化操作；Portal 不能替 OpenD 完成密码、验证码或设备认证。首次安装器**只启动 Trading Bot**；以下 OpenD 托管为独立可选步骤，安装它不会自动启动或抢占已运行的网关。

### 可选：OpenD 异常重启与开机恢复

1. 在官方 OpenD 10.10 或更新版本的交互登录中选择记住密码，并确认登录成功。已有 tmux 会话在线只能证明当前连接有效，不能证明记住登录或重启恢复有效。
2. 将官方 OpenD 目录权限设为 700，`OpenD.xml` 设为 600。XML 不保留明文密码、密码 MD5 或交易解锁信息；禁用 Telnet/WebSocket 监听。
3. 在 OpenD 自己的私有目录创建 `opend-service.json`，权限 600。内容为下例；`login_account` 是用户刚才采用的登录标识，不是模拟交易账户号。可选 `area_code` 仅用于手机号登录。此文件不能放进公共发布包。

```json
{
  "provider": "moomoo",
  "login_account": "YOUR_LOGIN_ACCOUNT",
  "remembered_login_confirmed": true
}
```

`remembered_login_confirmed` 是完成官方记住登录步骤后的记录，不能用于绕过登录验证。工具从不接受密码。官方启动参数包含登录标识，因此同一服务器用户可在进程参数中看到该标识；不要在共享用户下部署。

从项目目录检查和安装（Futu 改用 `--provider futu` 与对应目录）：

```bash
.venv/bin/python deploy/server/opend_service.py check --project "$PWD" --opend-dir /home/ubuntu/moomoo-opend --provider moomoo
.venv/bin/python deploy/server/opend_service.py install --project "$PWD" --opend-dir /home/ubuntu/moomoo-opend --provider moomoo
```

安装仅创建用户服务并重新加载 systemd。确认没有交易任务后，退出此前 tmux 中的 OpenD，再手动启动新服务；避免两个进程使用同一目录或端口。

```bash
systemctl --user start trading-bot-opend-moomoo.service
systemctl --user status trading-bot-opend-moomoo.service
```

然后在 Portal 检查模拟账户和行情访问。**服务 active 不等于账户已登录**。验证成功后才启用重启恢复：

```bash
systemctl --user enable trading-bot-opend-moomoo.service
sudo loginctl enable-linger "$(id -un)"
```

托管进程监听 loopback、不自动抢回行情权限、不接收键盘输入、不执行交易解锁。异常退出每隔 30 秒重启，10 分钟最多 3 次；配置不合格时停止自动重启。OpenD 需要重新验证、记住登录失效或服务未登录时，需要人工重新登录，不能靠反复重启修复。控制台日志仅保存在私有 OpenD 目录的 `service-output.log`；超过 10 MB 后在下次启动时轮换一次。OpenD 服务本身不能限定所有其他客户端的账户权限，模拟账户隔离由 Trading Bot 的券商模块强制执行。

资料：[OpenD CLI 官方配置与启动说明](https://openapi.moomoo.com/moomoo-api-doc/en/opend/opend-cmd.html)、[已有模拟盘接入说明](../brokers/README.md)。

## 可在休市期间测试

工作台登录/退出、后台开关、策略与模块切换、Massive 配置及数据下载、研究预览、模拟账户余额与持仓读取。准备交易时仍会核对行情、账户与市场状态；行情不合格或休市时不能据此验证真实成交。

发布校验应分别记录本地回归、Ubuntu 启动、官方登录、模拟账户读取和模拟成交。发布包本身不证明这些已通过；休市期间可验证时间窗口阻止下单，不能据此宣称模拟成交验证完成。

## 运维

在「系统 → 服务器」可下载只读诊断摘要，包括最近失败任务、自动计划状态、框架与券商账本的匹配计数。它不连接券商、不核对或改写订单，也不输出账号、密钥、原始券商响应。后台停止时仍可下载。命令行等价入口为：

```bash
.venv/bin/python deploy/server/diagnose.py --project "$PWD"
```

框架的 `unknown` 且没有 adapter 记录，只能说明两本账本尚未对应，不能据此断定订单未发送或重新下单。代码升级不会重新分类已有的不确定订单。任务失败后的诊断编号和代码位置位于「系统 → 操作记录」；历史版本未留下的信息无法补造。

账户历史、故障通知和独立监控见 [账户表现与监控](OBSERVABILITY.md)。升级不覆盖凭据或已有运行记录；新建的统计从首次成功查询开始。关闭自动交易仍可监控账户，整个服务器停机告警需单独配置外部心跳。

```bash
systemctl --user status trading-bot
journalctl --user -u trading-bot --since today
systemctl --user stop trading-bot
systemctl --user start trading-bot
```

服务异常时 systemd 可重启工作台。自动运行是否恢复取决于持久化的自动运行设置；待核对、传输结果不确定的订单需要先对账，不能盲目重新提交。停止服务前暂停自动运行并等待订单工作进程退出。后台偏好、订单与缓存位于服务器自己的 `runs/`。

检查 HTTPS 与续期：

```bash
sudo nginx -t
systemctl status nginx
systemctl list-timers trading-bot-cert-renew.timer
sudo /opt/trading-bot-certbot/bin/certbot renew --dry-run
```

网站始终要求工作台密码认证，登录 Cookie 仅通过 HTTPS 发送。API 还核对访问地址和操作令牌。单人 Portal 不提供多人角色管理。未来更换域名时，先将 DNS 指向实例，再迁移 HTTPS 地址并安装对应证书；账户配置与交易模块无需更改。

网页 OAuth 使用 PKCE、一次性 state 和本机加密令牌文件；密钥与令牌保存在同一服务器用户的私有目录。这能避免发布包或日志泄露明文令牌，不能防御已经取得服务器用户权限的攻击者。断开会清除本程序的授权文件；券商端授权需要去官网撤销。已有 Tiger 私钥配置不会因切换而自动删除。
