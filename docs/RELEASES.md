# 版本、下载与升级

## 当前推荐版本

- 应用版本：`0.12.0-pre.2`，预发布。
- Git 标签：`v2026.10.15-pre-release`，发布日期 2026-10-15。
- [发布页面](https://github.com/cjxkdc/trading-bot/releases/tag/v2026.10.15-pre-release)：包含安装包、升级工具、摘要与实际验证结果。
- [更新记录](../CHANGELOG.md)：本次变动与仍然存在的限制。

`main` 是继续开发的源码；标签固定一次发布的代码。安装包由该次发布源码与重新构建的网页组成。安装包中的 `ui/package.json` 可查看应用版本，`release-manifest.json` 可逐项核对文件摘要。工作台显示的网页 build 标识用于辨认构建，不等同于应用版本号。

## 选择下载内容

| 文件 | 用途 |
| --- | --- |
| `Trading-bot-desktop.zip` | Windows 本地安装，已带网页；通过 CMD 启动器运行 |
| `Trading-bot-server.zip` | Ubuntu / 云服务器安装，已带网页，服务器不用安装 Node |
| 对应的 `.zip.sha256` | 检查下载文件完整性 |
| `upgrade.py` 与 `upgrade.py.sha256` | 已有服务器校验升级所需的本版工具 |
| GitHub 的 Source code | 开发用源码，不包含已构建网页 |

本次桌面包没有 exe，安装后使用 `deploy/desktop/Start Local Portal.cmd`；已有安装的 exe 仍可用。两个安装包都不含 Python、虚拟环境或券商 SDK，按所选部署方式安装依赖。完整回归测试需要源码仓库，安装包不含测试策略夹具。

## 首次使用

**Windows：** 安装 Python 3.12，完整解压桌面 ZIP，运行 `deploy/desktop/Install Local.cmd`，按实际券商安装 SDK，再运行 `deploy/desktop/Start Local Portal.cmd`。在 Portal 的「连接」里配置自己的数据源与券商。详细命令见 [README](../README.md#首次运行windows)。

**Ubuntu：** 完整解压服务器 ZIP，按 [服务器指南](SERVER.md#首次安装) 安装服务、配置 Portal 登录与 HTTPS，再接入自己的券商和数据源。

Futu/moomoo OpenD 要单独安装、登录和绑定账户；Massive 需要自己的 API Key 与对应数据权限。下载项目不会连接项目维护者的券商、云服务器或账户，也不会自动开启交易。

## 已有安装升级

先暂停运行并等待当前任务结束，再备份自己的安装目录。必须保留 `secrets/`、`runs/`、已有 `configs/` 和 `.venv/`；新安装的默认配置不能替代已绑定账户和账本。

**Windows：** 完整退出工作台。将新桌面包解压到另一个目录，对照发布说明更新公共源码与网页，保留原配置和运行数据；本版本没有桌面自动升级器。若自行改过策略，先比较代码差异。当前版本未改变依赖，无需为这次整理重新建立虚拟环境。

**服务器：** 下载本版服务器 ZIP、独立 `upgrade.py` 及对应摘要，把升级工具放在安装目录之外。旧版升级工具不认识新增的 `CHANGELOG.md`、`PRODUCT.md`，使用本版工具进行预览校验，停止服务后再应用。完整步骤与回退命令见 [校验升级与回退](SERVER.md#校验升级与回退)。升级默认保留所有既有配置、密钥和业务状态。

文件摘要用于检查传输完整性；工具和摘要都应从上述仓库发布页取得。保留旧版本和升级备份，便于出现问题时回退。发布新包不会替你更新已经运行的本地或云端实例。

## 数据与能力范围

公开代码和发布包包含策略、介绍、默认参数和示例配置，不包含个人登录凭据、绑定账户、持仓、订单、收益历史或行情缓存。第三方策略来源见 [第三方声明](THIRD_PARTY_NOTICES.md)。

框架的券商、数据源和策略可替换，但各适配器支持的市场与交易能力仍有区别。当前 Portal 执行仅限模拟与本地回放，实盘链路未验收；Tiger 自动运行、港股适配器与 Massive 历史数据完整性等限制以 [更新记录](../CHANGELOG.md) 为准。
