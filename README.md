# Trading bot — 模块化交易研究 v0.5

策略、框架、平台分开维护，由配置选择并装配。目前提供科技股组合策略、QuantConnect 回测导出与报告比较、单标的 CSV 研究、本地模拟执行、Tiger 模拟账户执行和只读账户查询。

## 三块各自做什么

| 目录 | 职责 |
|---|---|
| `strategies/` | 选股、打分、目标仓位、声明需要的历史数据；只做计算 |
| `framework/` | 标准数据类型、决策核验、仓位与费用检查、订单状态和研究回测 |
| `platforms/` | 数据源、券商连接器、QuantConnect 接入；转换外部数据和订单 |
| `assembly.py` | 根据登记文件和配置装配三块 |
| `bot.py` | 列出模块、预览组合、运行本地模拟、导出回测 |

`platforms/data/` 放数据适配器，`platforms/brokers/` 放券商适配器，`platforms/quantconnect/` 放 QC 回测接入。平台登记与策略登记分别位于各自的 `plugins/`。旧模块路径保留轻量兼容导入。

换同一接口范围内的策略或平台，增加模块、登记并修改配置即可；期权、做空、多币种保证金或异步实盘执行等新能力，需要完善相应接口和执行模型。

## 默认策略与主配置

主配置为 [portfolio_config.json](portfolio_config.json)，同时供本地组合入口和 QC 导出读取。默认科技股盈利动量规则仍是：最多 5 只，每只 19%，过去 12 个月营业利润为正，成交额过滤，252 日跳过最近 21 日的动量，同一细分行业最多 2 只。月初调仓由 QC 接入的交易日历安排。

本次没有根据历史收益调优参数。可比较版本见 [策略说明](strategies/README.md)。`configs/equal_weight_control.json` 是接口演示，不是投资基准。

- `strategy`：选择策略及其参数。
- `data`：选择本地行情输入，目前为标准快照文件。
- `broker`：选择本地执行连接器，目前为 paper。
- `limits`、`fees`、`initial_cash`：仓位限制、费用假设、初始虚拟资金。
- `backtest`：QC 回测起止日期和滑点假设。

配置可通过 `extends` 引用另一个本地文件，路径相对当前配置；同名的顶层块整块替换。使用 `bot.py config` 可查看最终配置。`paper_config.json` 是指向主配置的兼容入口。`config.json` 专属于早期单标的研究。

## 从这里开始

需要 Python 3.11+。Windows 的纽约时区计算需要 tzdata：

```text
python -m pip install -r requirements.txt
python bot.py strategies
python bot.py platforms
python bot.py config
python bot.py demo --out runs/demo-001
```

demo 使用合成数据：只验证文件读取、选股、整股下单、费用和账户记录，不是市场回测。它建立独立本地账户，不连接老虎、Futu 或其他真实券商。

先预览目标持仓，再重复回放同一批数据：

```text
python bot.py plan --snapshot runs/demo-001/synthetic-snapshot.json
python bot.py paper --snapshot runs/demo-001/synthetic-snapshot.json --state runs/demo-001/state
```

预览不创建账户、不下单。相同账户、同一交易日重复运行已完成决策，不会再买一次；丢失账本、旧批次未完成或更换同日策略会阻止执行。一个账户只对应一个状态目录，不应复制或清空账本来绕过检查。

## 回测放在项目里面

科技股组合使用 QuantConnect/LEAN 的历史数据和回测引擎：

```text
python bot.py export-qc --out runs/qc-baseline-001
```

将生成的 `main.py` 全部复制到 QC 新 Python 项目，再点击 Backtest。该命令只导出；不会提交云端任务。每个输出目录附带配置与代码指纹，禁止覆盖旧实验。具体流程见 [回测说明](backtests/README.md)。

默认可复制文件也在 [platforms/quantconnect/main.py](platforms/quantconnect/main.py)。默认区间仍为 2020 年、初始资金 100,000 美元，每笔固定费用 1 美元、单边滑点 0.10%；它们是研究假设，未对应实际券商费率。

旧的单标的研究入口继续保留：

```text
python research.py screen --data SPY.csv --out runs/experiment-001
python research.py validate --data SPY.csv --out runs/experiment-001
python research.py holdout --data SPY.csv --out runs/experiment-001
```

数据为 `date,total_return_index`，必须核对总回报序列的分红/拆股口径并提供预热历史。`config.json` 定义开发、验证和最终测试区间；阶段之间冻结数据、配置、源码。该模型按连续比例敞口和收盘总回报估计，不能用于科技股多股票组合或冒充真实券商撮合。

## v0.4 新增

- `import-qc`：导入 QC JSON，保存原始报告、配置、代码副本和校验值；总收益与年化分开。
- `compare-qc`：生成 Markdown/JSON 对比，显示不同年份、成本及完成状态；不自动选“最佳策略”。
- `export-qc --start --end --slippage-bps --note`：独立导出不同区间和成本假设，先记录研究问题。
- `broker-status`：默认离线检查 Tiger 配置；加 `--connect` 只查询账户、持仓及未完成订单。
- `PortfolioContext`：策略可以选择接收冻结的持仓名单，为排名缓冲留出接口；当前策略未增加新规则。

具体用法见 [回测流程](backtests/README.md)、[Tiger 接入](platforms/brokers/README.md) 和 [策略接口](strategies/README.md)。

## v0.5：Tiger 模拟券商

新增独立 `tiger_paper` broker；选股模块不变。券商操作默认使用 [tiger_paper_config.json](tiger_paper_config.json)，继承原策略，凭据位于 `secrets/tiger_paper/`。`portfolio_config.json` 和 `paper` 命令继续用于离线研究/本地回放。

```text
.venv\Scripts\python.exe bot.py broker-status
.venv\Scripts\python.exe bot.py broker-status --connect
```

模拟账户仅接受 17 位模拟账号，并要求 Tiger 返回 `account_type=PAPER`。所有 API 请求都检查账户归属，真实账号在客户端初始化前即被拦截。没有实盘执行入口。

`broker-paper --snapshot <当日真实行情快照>` 可执行一轮模拟调仓。订单尚未成交时暂停，保留订单账本，下次核对已有订单，不重复发送。同一框架负责策略计算和风控，Tiger 平台模块负责实际的模拟订单。当前还没有自动行情订阅、完整实时股票池或持续运行的调度器。

2026-10-02 已验证 Tiger 模拟账户查询，以及 1 股 AAPL、限价 1 美元的模拟测试单：已接受 → 已撤销，成交 0，剩余挂单 0。这是连接验证，不是策略表现验证；部分成交与断线恢复另有离线测试。

## v0.3 已有基础

- 运行入口不再直接构造特定数据源和券商，统一注入 PortfolioRunner。
- 数据和券商连接器也按登记文件加载，分别选择。
- 主配置供本地组合运行和 QC 导出共用；导出不包含券商/数据连接参数。
- 有效策略参数包括构造函数默认值，都进入决策记录。
- 历史长度由策略声明；QC 不再写死 253 根日线，也不再替策略进行盈利筛选。
- 增加只读组合预览、实验导出与两个单因素比较配置。
- 保留订单意图先落盘、先减后加、重复执行保护和不确定订单阻断。

## 当前能力边界

| 路径 | 已完成 | 尚待完成 |
|---|---|---|
| 策略研究 | 规则实现、可替换配置、独立对照 | 多年份及独立验证，证明改动有效 |
| QC 回测 | 导出、报告存档与对比 | v0.4 云端实际运行与报告核对 |
| 本地 paper | 文件报价立即全额成交、SQLite 状态恢复 | 连续实时行情、结算、公司行动、真实盘口 |
| Tiger 模拟券商 | 账户查询、限价下单/查询/撤单、持久化订单、防重复、实盘拦截；已联网验证提交与撤单 | 连续行情、完整策略运行、公司行动、取消批次后的人工处置流程 |

执行器接受本地 paper 和 broker_paper 两种模拟模式，拒绝 live。异步未完成订单会暂停批次，拒单或撤单不会自动重新计算/重发剩余交易。更换平台时共用策略计算，不保证不同数据口径和撮合模型得到相同收益。

运行检查：

```text
python tools/build_quantconnect.py
python -m unittest discover -s tests
```

完整历史表适配检查还需 pandas；QC 环境自带。本地没有 pandas 时相关检查会显式跳过，其余检查使用标准库。

开发时运行 `python -m pip install -r requirements-dev.txt -r requirements-tiger.txt` 可安装完整测试依赖。Tiger 是可选安装；未安装时只跳过实际 SDK 契约检查。GitHub Actions 在 Linux 和 Windows 上安装两份依赖、执行全部检查，并核对已提交的 QC 单文件是否与源码一致；这些检查不会连接券商或提交云端回测。

Git 仅管理代码、示例配置和文档。`runs/`、本地账户 SQLite、`.env`、密钥和虚拟环境均忽略。上传到 GitHub 不会启动程序；运行和后续服务器部署需单独配置。

后续顺序：导出当前基准 → 核对 QC 完整回测 → 固定方案做多年份与成本比较 → 券商模拟账户验证 → 再单独准备实盘。服务器和完整 UI 暂时不是必需项。

