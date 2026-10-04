# Python 依赖清单

这些文本文件记录程序依赖的软件库及版本，由 `pip` 安装到 `.venv`。它们不包含账户、密码或交易数据。现有环境已经安装的依赖可继续使用，整理清单不需要重装。

从项目根目录运行对应命令：

| 用途 | 命令 |
|---|---|
| 面板与基础框架 | `.venv\Scripts\python.exe -m pip install -r requirements.txt` |
| 使用 Futu | `.venv\Scripts\python.exe -m pip install -r requirements/futu.txt` |
| 使用 moomoo | `.venv\Scripts\python.exe -m pip install -r requirements/moomoo.txt` |
| 使用 Tiger | `.venv\Scripts\python.exe -m pip install -r requirements/tiger.txt` |
| 网页授权 / 服务器 Portal | `.venv\Scripts\python.exe -m pip install -r requirements/server.txt` |
| 开发与完整测试 | `.venv\Scripts\python.exe -m pip install -r requirements/dev.txt` |
| 构建 Windows exe | 在独立打包环境运行 `python -m pip install -r requirements/build.txt` |

券商清单自动包含基础运行依赖；开发清单包含三家券商 SDK 和测试依赖。日常使用只需基础依赖与实际使用的券商。Massive 接口使用 Python 标准库，无单独依赖清单。

`-r` 表示引用另一份清单，路径相对于引用文件解析；库的版本号只在所属清单维护一次。`==` 固定直接依赖版本；这不是完整的传递依赖锁文件。这里保留原有版本，本次整理没有升级或降级已安装的软件包。

网页前端的构建依赖仍在 `ui/package.json` / `ui/package-lock.json` 中，使用 Node/npm 管理；日常运行已经构建好的页面不需要 Node。
