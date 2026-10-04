# Trading Bot

**已安装环境的日常使用：双击本文件夹里的 `Trading Bot.exe`，在浏览器 Portal 中操作。**

## 首次运行（Windows）

GitHub 提供源码，不包含 exe、已构建网页或 Python 虚拟环境。桌面发布 ZIP 包含已构建网页；是否包含 exe 取决于打包时是否提供启动器。两种方式都需要自己的券商登录与数据源配置。

1. 安装 Python 3.12，解压完整项目，在项目根目录运行 `deploy/desktop/Install Local.cmd`。
2. 选择实际使用的券商，安装对应 SDK。例如 moomoo：

   ```powershell
   .venv\Scripts\python.exe -m pip install -r requirements/moomoo.txt
   ```

   Futu 使用 `requirements/futu.txt`，Tiger 使用 `requirements/tiger.txt`。
3. **仅从 GitHub 下载源码时**，另安装 Node.js（至少 22.12），在项目根目录构建网页：

   ```powershell
   npm --prefix ui ci --ignore-scripts
   npm --prefix ui run build
   ```

   使用已带 `ui/dist/` 的发布包可跳过这一步，日常运行不需要 Node。
4. 运行 `deploy/desktop/Start Local Portal.cmd`；发布包带有 exe 时，也可双击根目录启动器。
5. 在工作台配置自己的数据源；Futu/moomoo 需要另外安装官方 OpenD，登录自己的账户并绑定模拟账户。Massive 需要自己的 API Key，数据权限必须满足所选策略。连接 OpenD 不会替代策略所需的完整选股数据。

先通过首页「一键测试并准备」检查环境、账户和数据，再按结果进行模拟操作。自动运行需要单独保存计划并开启。服务器部署使用发布包及 [服务器指南](docs/SERVER.md)；从源码部署同样须先构建网页。

## 日常操作

右上角开关控制交易后台。关闭网页不等于关闭后台；完整退出在「运行与日志」。当前 Portal 仅启用模拟账户与本地回放。

## 准备一次模拟交易

选好策略、模拟券商和数据源后，在「总览」点击 **一键测试并准备**。程序依次检查账户、数据配置与权限、所需数据、选股结果和模拟订单准备条件；每一步会保留结果，不需要手动逐项点击。

- 每次都会按当前策略检查并采集数据；数据模块会复用有效缓存，减少重复下载历史行情。更换策略后，按新策略的数据要求重新检查。
- 周末也可以运行检查、更新数据并查看选股。交易时段或报价不满足要求时，订单准备会显示具体原因，已完成的检查仍然可见。
- 检查和准备都不会发送订单。只有出现「提交模拟订单」后，核对当前模拟账户、股票、数量和限价，再确认提交。准备结果会过期，过期后重新检查。
- 下载期间可用「停止下载」请求结束当前采集；正在等待的网络请求可能先返回。停止后不会继续准备订单，已下载的缓存保留。
- 数据缺失、权限不足或其他限制会保留在结果中；完成选股不代表已满足交易条件。「分步操作」仍可单独更新、预览或准备。

此按钮不会开启定时运行，也不会更改已有自动运行设置。维护或只想手动测试时，先在「运行与日志」关闭自动运行；后台电源与自动运行是两个独立开关。

## 文件放在哪里

| 目录 / 文件 | 用途 |
|---|---|
| `Trading Bot.exe` | 日常启动入口 |
| `configs/` | 策略参数、券商实例和数据源配置 |
| `strategies/` | 可替换策略，包括科技股动量与 MOM8 适配版 |
| `brokers/` | 可替换券商接口 |
| `data/` | 可替换数据源接口 |
| `framework/` | 公共运行、风控和订单恢复 |
| `backtests/` | 回测与 QuantConnect 接入 |
| `app/` | 程序装配、启动器、命令行工具 |
| `portal/`、`ui/` | 面板服务和网页界面 |
| `docs/` | 使用指南、架构与第三方说明 |
| `runs/` | 缓存、实验、运行记录、账户账本及备份 |
| `secrets/` | 本机密钥和账户连接设置，不上传 Git |
| `tests/`、`tools/`、`requirements/` | 开发检查、构建工具和依赖清单 |
| `.venv/` | 程序的 Python 运行环境 |
| `bot.py` | 保留的命令行入口 |

配置和启动代码已归档到对应目录。`runs/`、`secrets/` 和 `.venv/` 保持原位；不要单独移动 exe，也不要清空账户账本。

## 常用说明

- [完整使用指南](docs/guide.md) · [架构](docs/architecture.md)
- [配置索引](configs/README.md) · [命令行入口](app/README.md)
- [Portal 安装与模块接入](portal/README.md) · [Python 依赖](requirements/README.md)
- [Massive 接入](data/MASSIVE.md) · [MOM8 规则与适配差异](strategies/MOM8.md)
- [第三方声明](docs/THIRD_PARTY_NOTICES.md)
- [AWS 与远程 Portal](docs/SERVER.md) · [桌面 / 服务器发布包](deploy/README.md)

服务器模式通过 HTTPS 网站访问，保留独立的工作台登录；切换券商后重新核验当前账户。固定 IP 或域名均可使用，不需要本机隧道。`moomoo 网页沙盒` 是只读授权验证接口，不提供下单；OpenD 模拟盘保持独立。

「运行与日志」里的自动运行开关可启用定时模拟调仓，默认关闭。后台电源开关与自动运行开关各自独立。详见 [自动模拟交易](docs/AUTOMATION.md)。服务器包包含可选 OpenD 托管工具，安装更新不会替你开启交易或改动登录凭据。

在项目根目录运行命令，例如 `python bot.py config`；依赖安装仍使用 `python -m pip install -r requirements.txt`。
