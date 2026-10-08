# Trading bot — 五模块交易研究 v0.11

项目按 **券商、数据、策略、框架、回测** 五个模块维护。策略、数据来源和券商分别登记、分别选择。Portal 按模块声明生成选项、参数和操作；公共框架和 QuantConnect 回测接入保持固定。

策略库中的每个模块提供独立的规则、参数和数据说明，见 [策略目录](../strategies/README.md)。选择策略后，在其工作区查看说明、调整参数、开展回测或创建运行实例。

## Windows 双击启动

打开根目录的 **Trading Bot.exe**。浏览器会打开本地工作台，后台服务独立运行。首次默认券商为 Futu 模拟盘、数据源为 Massive；之后记住你最后选择的模块。

1. 「模块配置 → 策略」查看兼容策略、模块参数及 README。
2. 「模块配置 → 数据」选择数据源。页面只显示该模块支持的配置、采集、权限检查和快照导入操作。
3. 「模块配置 → 券商」选择账户模块并检查连接。账户配置和账本沿用该模块的独立目录。
4. 「策略运行」创建实例；只想执行一次时，使用其「手动调仓」子页。只有数据与账户满足执行要求，才可确认提交模拟订单。

Massive 的密钥、限速和缓存说明见 [Massive 接入说明](../data/MASSIVE.md)。本地回放账户使用合成或历史快照，不连接券商。原命令行离线演示仍可使用。
下载产生**研究预览**：显示选股结果、覆盖数、缺失项、价格截止日期和数据口径。它没有券商实时执行报价，不启用交易按钮。已有完整标准执行快照仍可通过「数据中心 → 导入快照 → 预览」核验后，手动执行一轮模拟交易。

右上角常驻「交易后台开关」：绿色表示后台响应正常，灰色表示已停止，黄色表示状态待核对；左下角显示最近检查时间。关闭交易后台后面板仍能使用，可直接再次开启。有任务执行时不可直接关闭；开关会记住关闭状态。开启后台只代表就绪，不会自动连接账户或下单。

首页只显示运行摘要和需处理事项。账户表现与 K 线在「账户 → 账户表现」，回测在「研究」，资源和日志在「系统」。关闭网页不会停止已开启的后台；本地「系统 → 运行与日志 → 退出整个工作台」同时退出面板服务和交易后台。新机器安装、前端构建和详细功能见 [本地工作台说明](../portal/README.md)。

Massive 使用 SIC 行业分类、拆股复权日线，与原 QC 的股票池及价格口径不同；原策略规则仍然共用，但选股结果不能直接宣称复现 QC 回测。采集、模拟订单准备与自动调度已实现，实际可运行范围取决于自己的数据权限、覆盖和券商连接检查。

券商实例来自 `configs/broker_profiles.json`，数据实例来自 `configs/data_sources.json`，新模块也可在登记文件中声明默认实例。默认 Futu 模拟实例沿用原配置及账本。切换模块会清除旧预览；切换账户还会清除旧账户显示。现有实盘接口可见但不可选，Portal 的执行权限仍仅限模拟与本地回放。

密钥存放在 `secrets/`，行情缓存及研究结果在 `runs/data/`，面板日志和实验在 `runs/portal/`，均被 Git 忽略。exe 调用同目录 `.venv/Scripts/python.exe` 和项目源码；请保留整个项目目录。

重建 exe：

```text
python -m pip install -r requirements/build.txt
python tools/build_launcher.py
```

依赖清单统一存放在 `requirements/`，根目录仅保留日常运行所需的 `requirements.txt`，说明见 [Python 依赖](../requirements/README.md)。

把 `dist/Trading Bot.exe` 复制到项目根目录。打包只包含启动器，不包含密钥、账本、网页资源或券商 SDK；前端需另行构建到 `ui/dist`。

## 五个模块

| 目录 | 职责 |
|---|---|
| `brokers/` | Futu、moomoo、Tiger、本地模拟的账户与订单适配 |
| `data/` | Massive、Futu 等行情和财报获取、缓存、校验与标准化 |
| `strategies/` | 筛选股票、打分、生成目标权重，不直接操作券商或数据 API |
| `framework/` | 公共契约、决策、风控、订单生命周期和恢复 |
| `backtests/` | 本地单标的历史引擎、QC 适配、回测结果核验与比较 |

`app/assembly.py` 是交易装配入口；`app/module_registry.py` 负责 Portal 模块发现与实例装配；`app/portal_worker.py` 按所选模块执行操作。`bot.py`、`app/market_data.py` 是命令行入口，`app/launcher_worker.py` 保留给旧窗口。策略、数据、券商分别在各自的 `plugins/` 目录登记。`platforms/` 保留旧导入兼容路径和厂商 SDK 公共配置工具，现有命令、配置和账本继续使用。

Portal 的选择分别由主配置中的 `strategy`、`configs/data_sources.json`、`configs/broker_profiles.json` 提供。命令行继续使用各自配置中的 `broker` 和 `data`。同一接口范围内切换插件不需要修改框架；期权、做空或多币种等新能力可能需要扩展通用契约。

## 默认策略与主配置

主配置为 [configs/portfolio_config.json](../configs/portfolio_config.json)，同时供本地组合入口和 QC 导出读取。默认科技股盈利动量规则仍是：最多 5 只，每只 19%，过去 12 个月营业利润为正，成交额过滤，252 日跳过最近 21 日的动量，同一细分行业最多 2 只。月初调仓由 QC 接入的交易日历安排。

组合规则见 [策略目录](../strategies/README.md)，预装策略仅包含 MOM8 动量轮动和科技股盈利动量。

- `strategy`：选择策略及其参数。
- `data`：选择本地行情输入，目前为标准快照文件。
- `broker`：选择本地执行连接器，目前为 paper。
- `limits`、`fees`、`initial_cash`：仓位限制、费用假设、初始虚拟资金。
- `backtest`：QC 回测起止日期和滑点假设。

配置可通过 `extends` 引用另一个本地文件，路径相对当前配置；同名的顶层块整块替换。使用 `bot.py config` 可查看最终配置。`configs/paper_config.json` 是指向主配置的兼容入口。单标的研究需自行提供配置文件和已登记的策略插件。

## 从这里开始

需要 Python 3.11+。默认清单安装 Portal 与基础框架依赖；券商 SDK 按需安装，见 [依赖说明](../requirements/README.md)。Windows 时区数据也包含在默认清单中：

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

将生成的 `main.py` 全部复制到 QC 新 Python 项目，再点击 Backtest。该命令只导出；不会提交云端任务。每个输出目录附带配置与代码指纹，禁止覆盖旧实验。具体流程见 [回测说明](../backtests/README.md)。

默认可复制文件也在 [platforms/quantconnect/main.py](../platforms/quantconnect/main.py)。默认区间仍为 2020 年、初始资金 100,000 美元，每笔固定费用 1 美元、单边滑点 0.10%；它们是研究假设，未对应实际券商费率。

旧的单标的研究入口继续保留：

```text
python -m app.research screen --config my-research.json --data SPY.csv --out runs/experiment-001
python -m app.research validate --config my-research.json --data SPY.csv --out runs/experiment-001
python -m app.research holdout --config my-research.json --data SPY.csv --out runs/experiment-001
```

数据为 `date,total_return_index`，必须核对总回报序列的分红/拆股口径并提供预热历史。自备研究配置定义候选策略、开发、验证和最终测试区间；阶段之间冻结数据、配置、源码。该模型按连续比例敞口和收盘总回报估计，不能用于科技股多股票组合或冒充真实券商撮合。

## v0.4 新增

- `import-qc`：导入 QC JSON，保存原始报告、配置、代码副本和校验值；总收益与年化分开。
- `compare-qc`：生成 Markdown/JSON 对比，显示不同年份、成本及完成状态；不自动选“最佳策略”。
- `export-qc --start --end --slippage-bps --note`：独立导出不同区间和成本假设，先记录研究问题。
- `broker-status`：默认离线检查 Tiger 配置；加 `--connect` 只查询账户、持仓及未完成订单。
- `PortfolioContext`：策略可以选择接收冻结的持仓名单，为排名缓冲留出接口；当前策略未增加新规则。

具体用法见 [回测流程](../backtests/README.md)、[Tiger 接入](../brokers/README.md) 和 [策略接口](STRATEGY_MODULES.md)。

## v0.5：Tiger 模拟券商

新增独立 `tiger_paper` broker；选股模块不变。券商操作默认使用 [configs/tiger_paper_config.json](../configs/tiger_paper_config.json)，继承原策略，凭据位于 `secrets/tiger_paper/`。`configs/portfolio_config.json` 和 `paper` 命令继续用于离线研究/本地回放。

```text
.venv\Scripts\python.exe bot.py broker-status
.venv\Scripts\python.exe bot.py broker-status --connect
```

Tiger 模拟账户仅接受 17 位模拟账号，并要求 Tiger 返回 `account_type=PAPER`。该模块所有 API 请求都检查账户归属，真实账号在客户端初始化前即被拦截。Tiger 目前没有实盘模块。

`broker-paper --snapshot <当日真实行情快照>` 可执行一轮模拟调仓。订单尚未成交时暂停，保留订单账本，下次核对已有订单，不重复发送。同一框架负责策略计算和风控，Tiger 平台模块负责实际的模拟订单。当前还没有自动行情订阅、完整实时股票池或持续运行的调度器。

2026-10-02 已验证 Tiger 模拟账户查询，以及 1 股 AAPL、限价 1 美元的模拟测试单：已接受 → 已撤销，成交 0，剩余挂单 0。这是连接验证，不是策略表现验证；部分成交与断线恢复另有离线测试。

### Tiger 行情检查

新增独立 `tiger_market` 数据检查模块，可复查权限、读取单只股票的日线、报价和营业利润，并保存结果：

```text
.venv\Scripts\python.exe bot.py market-status --connect --out runs/market-check-001
```

默认不会抢占其他 API 设备的行情访问权。诊断结果不是完整股票池，不能直接交给 `broker-paper`。完整接入所需的数据、权限和命令见 [行情接入说明](../data/README.md)。

## v0.6：Futu 香港与 moomoo AU

新增 `futu_paper`、`moomoo_paper` 两个模拟券商和对应的独立行情检查模块。分别使用 `configs/futu_paper_config.json`、`configs/moomoo_paper_config.json`；共用原策略，独立连接设置、端口和订单账本。

```text
python bot.py broker-status --config configs/futu_paper_config.json
python bot.py broker-status --config configs/moomoo_paper_config.json
```

以上默认离线。paper 模块仅允许 SIMULATE，支持美股整股限价单、查询、撤单和订单恢复。Futu 已于 2026-10-02 完成模拟账户绑定、资金/持仓/挂单及 AAPL 元数据与快照的联网查询；支持当前美股股票/期权合并模拟账户，但仍只用现金交易股票/ETF。模拟订单生命周期及 moomoo 登录仍待联网验收。完整股票池与基本面数据转换仍待实现，行情诊断不能直接作为调仓输入。安装、端口配置和后续登录步骤见 [Futu/moomoo 接入说明](../platforms/opend.md)。

## v0.7：独立实盘接口

新增 `futu_live` 和 `moomoo_live`，与各自 paper 模块共用订单实现和执行框架；四套配置继承同一策略，账本按平台和交易环境分开。实盘必须填写账户/登录 ID，由券商确认区域、账户类型和 US 权限，并验证所交易标的是股票/ETF。

`broker-status --config configs/futu_live_config.json` 默认只作离线检查；加 `--connect` 也只读。真正执行使用独立 `broker-live` 命令和 `--enable-live` 参数，没有实盘默认配置。只读检查不会解锁或交易。实盘连接尚未验收，未发送实盘订单，使用步骤及 GUI/命令行 OpenD 的解锁区别见 [接入说明](../platforms/opend.md)。

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
| Futu / moomoo 模拟券商 | 独立配置与账本、SIMULATE 限价单/查询/撤单、离线 SDK 与恢复测试；Futu 账户只读验证、科技股数据采集与选股预览 | moomoo 登录与权限核验、模拟订单联网验收、可执行的科技股数据时点及身份核验 |
| Futu / moomoo 实盘接口 | 独立 REAL 账户、只读查询、显式启用后的限价单/查询/撤单/解锁、标的类型验证；离线测试 | 本人登录、实盘接口与费用/结算验收、完整数据源与持续运行 |

执行器接受本地 paper、broker_paper，以及明确启用的 live 模式。异步未完成订单会暂停批次，拒单或撤单不会自动重新计算/重发剩余交易。更换平台时共用策略计算，不保证不同数据口径和撮合模型得到相同收益。

运行检查：

```text
python tools/build_quantconnect.py
python -m unittest discover -s tests
```

完整历史表适配检查还需 pandas；QC 环境自带。本地没有 pandas 时相关检查会显式跳过，其余检查使用标准库。

开发时运行 `python -m pip install -r requirements/dev.txt` 可安装完整测试依赖。券商 SDK 都是可选安装；缺失时相关实际 SDK 契约检查会跳过。GitHub Actions 在 Linux 和 Windows 上安装全部测试依赖，并核对 QC 单文件是否与源码一致；这些检查不会连接券商或提交云端回测。

Git 仅管理代码、示例配置和文档。`runs/`、本地账户 SQLite、`.env`、密钥和虚拟环境均忽略。上传到 GitHub 不会启动程序；运行和后续服务器部署需单独配置。

后续顺序：导出当前基准 → 核对 QC 完整回测 → 固定方案做多年份与成本比较 → 券商模拟账户验证 → 再单独准备实盘。本地 Portal 已提供操作入口；云端部署与持续调度需另行配置。
