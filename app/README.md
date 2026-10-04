# 程序入口

日常使用根目录的 `Trading Bot.exe`。本目录集中放启动、装配与命令行代码，五个业务模块仍分别维护。

以下命令均在项目根目录运行：

```text
python bot.py config
python bot.py strategies
python -m app.portal_launcher
python -m app.market_data --help
python -m app.research --help
```

`app/assembly.py` 装配交易模块，`app/module_registry.py` 负责 Portal 模块发现；`app/portal_server.py`、`app/portal_worker.py` 分别负责服务入口与单次任务。

`python -m app.launcher` 也打开同一个 Portal。`app/launcher_worker.py` 的旧函数仅供兼容测试；日常命令使用共用工作台接口。

这些命令已改用包入口 `python -m app.<名称>`，不要直接双击内部 Python 文件。

`app/workbench.py` 为网页与命令行提供同一套任务服务。使用 `python bot.py workbench view` 查看所选模块、任务和预览，其他操作见 `portal/README.md`。后台需先运行；CLI 不另建交易进程或自动选择券商。
