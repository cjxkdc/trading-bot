# 本地工作台

双击根目录的 `Trading Bot.exe`。浏览器打开面板，重复启动复用已有后台。关闭网页后已开启的后台仍在；「运行与日志 → 退出整个工作台」可退出面板服务与空闲后台。

Portal 是五个交易模块之上的控制层。策略、券商和数据分别选择，名称、表单和操作来自模块登记。公共组合框架与 QuantConnect 回测接入暂时固定。

## 日常使用

1. 「策略库」选择兼容的组合策略并保存参数。
2. 「数据中心」选择数据供应商或本地快照，查看当前策略要求的范围、历史长度和字段。Massive 只有一个入口，切换策略无需更换供应商。仅显示模块支持的配置、采集、权限检查、快照导入等操作。
3. 「券商与账户」选择账户模块。切换不会连接或下单，点击检查才读取账户。
4. 首页预览选股。研究快照不能执行；可执行快照还需框架验证、账户检查和一次明确确认。

策略与数据切换会清除旧预览；券商切换还会清除旧账户显示。预览绑定当前模块与配置，改动连接配置后必须重新预览。执行授权仅使用一次，失败不会自动重新下单。

首次默认值来自 `configs/data_sources.json` 和 `configs/broker_profiles.json`，之后记住 `runs/portal/preferences.json` 中的选择。策略保存到 `configs/portfolio_config.json`，原文件备份到 `runs/portal/config-history`。

账户查看和运行配置支持本地回放、券商模拟与实盘。订单执行按模块能力与具体账户授权检查；实盘授权默认空，当前账户仍使用模拟环境。支持手动与计划运行，详见 [模块化执行](../docs/MODULAR_EXECUTION.md) 和 [自动运行](../docs/AUTOMATION.md)。

## 后台开关与状态

右上角开关控制独立的交易后台进程，面板服务始终保留，因此关闭后还能从同一网页再次开启。页面每 2.5 秒读取进程与健康检查结果：

- 绿色「后台运行中」：进程存活且健康接口正常响应；下方区分等待操作和任务执行中。
- 灰色「后台已停止」：已确认交易进程退出，面板仍然可用。
- 「启动中 / 停止中」：正在等待真实状态变化，不能重复操作开关。
- 黄色「未响应 / 状态未知 / 异常退出」：没有把未知状态显示成已停止，也不会自动重复启动。

左下角显示最近检查时间。连接证券账户与运行交易后台是两个独立状态。后台电源与自动运行计划独立；仅开启后台不会开启自动交易。已开启且绑定仍有效的自动计划按保存状态恢复检查，手动预览授权不会跨重启恢复。执行中的任务会阻止关闭；程序不强杀下单进程。异常退出后需要手动开启，并核对先前操作记录。

开关状态保存在 `runs/portal/runtime-preferences.json`，已关闭的后台不会在下次双击启动器时自动开启。退出整个工作台不改变此偏好。

`app/portal_server.py` 运行面板控制服务，`portal/controller.py` 保留会话和固定控制接口；`portal/runtime.py` 管理独立后台进程并转发已授权的模块操作。两层仅监听本机、分别验证会话；后台私有令牌仅通过进程间管道传递。面板退出或失联时，后台等待正在执行的任务完成后退出，不重新发送订单。

## 接入新模块

登记目录为 `strategies/plugins/`、`data/plugins/`、`brokers/plugins/`。模块是受信任的本地代码，不是安全沙箱。新模块需要自己的实现或复用已有适配器，并增加登记 JSON；不需要在 Portal 中增加供应商名称分支。

### 策略

实现框架的 `PortfolioStrategy`，声明 `history_requirements`，登记 `type`、`kind: "portfolio"`、`version`、`entrypoint` 和 `input_schema`，例如：

```json
{
  "type": "my_selection",
  "kind": "portfolio",
  "version": "1.0",
  "entrypoint": "strategies.my_selection:MySelection",
  "input_schema": "us_technology_daily_v1",
  "data_requirements": {
    "universe": "technology",
    "fields": ["adjusted_closes", "industry", "dollar_volumes"],
    "conditional_fields": [
      {"parameter": "require_profit", "equals": true, "fields": ["operating_income_ttm"]}
    ]
  },
  "display_name": "我的选股策略",
  "description": "策略的具体规则",
  "parameters": {"lookback": {"label": "观察天数", "min": 1, "step": 1}}
}
```

参数名和默认值从构造函数读取；`parameters` 覆盖标签、类型、显示比例等。支持 string、number、integer、boolean、json 字段。`limit_rules` 可声明参数乘积不得超过框架限额，运行时仍检查实际目标权重。

上例假设策略有 `require_profit` 构造参数；无条件规则可省略 `conditional_fields`。`data_requirements` 声明范围和字段，历史根数取自 `history_requirements`；可另声明 market、security_type、frequency，默认美国普通股日线。添加范围或字段时，数据适配器也必须明确支持；声明需求不等于供应商已经实现。

单标的接口与当前组合框架不兼容，页面会显示原因。QC 导出仍要求满足当前适配器的数据与策略契约；换资产类型可能需要扩展接口。

### 数据

登记输出格式、支持的 `strategy_schemas` 列表（旧单格式 `strategy_schema` 仍兼容）和 `control`，控制类位于 `data/`。支持按需请求的适配器声明 `control.request_driven: true`：

| 成员 | 契约 |
|---|---|
| `for_request(request)` / `validate_request()` | 按需模块绑定 DataRequirements / 在联网前拒绝不支持的范围、字段或历史 |
| `directory` | 自己的 runs 数据目录；latest.json 指向最新文件 |
| `configuration_status()` | 不联网的配置摘要，不返回密钥 |
| `config_revision()` | 影响预览的配置摘要，不返回原始凭据 |
| `metadata(raw)` | 校验原始格式，返回日期、覆盖数、限制及研究/合成标记 |
| `load(raw)` | 返回 control_contracts.SnapshotInput，含标准股票、标签和可选执行快照 |
| `collect(out, price_bars, progress)` | 采集并返回快照路径 |
| `inspect(price_bars, progress)` | 在线权限检查，返回脱敏结果 |
| `configure(values)` / `cancel()` | 保存自己的设置 / 请求停止采集 |

`control.capabilities` 声明实际支持的 collect、inspect、configure、cancel、upload、preview；`control.settings_fields` 生成设置表单，敏感字段用 `secret: true`。`control.entrypoint` 指向控制类，`default_profile` 提供默认实例；显式实例配置保存在 `configs/data_sources.json`。

标准执行快照可复用 `data.controls:SnapshotControl`，相同研究格式可复用 `CurrentResearchControl`。供应商鉴权和格式转换留在自己的适配器内。研究输入固定不可执行。

`data.request_control:SnapshotProviderControl` 统一处理当前两种本地文件契约；`MassiveProviderControl` 验证需求与研究快照匹配。Portal 展示后端返回的需求描述，不按具体供应商或策略名称分支。旧 Massive 和本地文件选项通过登记中的 `profile_aliases` 映射到统一实例，已存数据保留。

### 券商

实现原 Connector/Connection 账户与订单契约。控制类位于 `brokers/`，提供 `inspect(connect)`、`session(cfg, snapshot, fees)`、`config_revision()` 和 `journal_path`；可继承 `brokers/controls.py` 的基类扩展自己的绑定检查。

`control` 登记入口、mode、capabilities、order_capabilities 与 default_profile。订单能力描述市场、资产、币种、订单种类、股数类型和有效期，不列策略输入格式。`configs/broker_profiles.json` 可登记多个实例，或用 config_file 引用原券商配置，避免复制绑定信息。每个联网账户必须沿用唯一状态目录，不能另建账本绕过恢复检查。

下面只增加一份登记，就能复用现有实现建立另一个本地回放模块：

```json
{
  "type": "practice_account",
  "kind": "broker",
  "version": "1.0",
  "entrypoint": "brokers.paper:PaperConnector",
  "display_name": "练习回放账户",
  "description": "独立本地资金与账本，不连接券商",
  "control": {
    "entrypoint": "brokers.controls:LocalPaperControl",
    "mode": "paper",
    "capabilities": ["inspect", "execute"],
    "supported_schemas": ["us_technology_daily_v1"],
    "default_profile": {"state_dir": "runs/practice-account"}
  }
}
```

没有显式实例的模块会用自己的 default_profile 自动出现在列表中。改登记后刷新页面，改 Python 实现后重启后台。新券商仍需各自完成联网验收，出现在列表中不代表已经连接。

## QC、文件与访问

回测页导出冻结代码供 QuantConnect 运行，导入 QC JSON、核验和比较结果，不自动提交云端回测。此页只列面板创建的实验，原命令行实验保留原位置。

订单从所选适配器的 journal_path 只读查询，不初始化或重置账本。其他程序或手工下的单不一定在本程序账本内。服务重启不恢复执行授权，也不重放任务。

`ui/src` 是页面，`portal/` 是 HTTP 与进程控制层，`app/workbench.py` 是共用任务与状态服务，`app/module_registry.py` 负责模块发现与实例装配，`app/portal_worker.py` 调用选定模块。后台只监听 127.0.0.1，验证 Host、Origin 和会话令牌；仅公开编译后的 UI，不开放项目目录。凭据通过本机接口和子进程 stdin 传递，不进入命令行、返回值或任务日志。

密钥位于 `secrets/`，行情缓存位于 `runs/data/`，偏好、脱敏日志和实验位于 `runs/portal/`，均被 Git 忽略。公开云端访问还需认证与 HTTPS。

## 安装和构建

完整项目需要 Python 虚拟环境及所需券商 SDK，网页依赖见 `requirements.txt`。构建前端需要 Node.js 22.12+，日常运行不需要 Node：

```powershell
.venv\Scripts\python.exe -m pip install -r requirements.txt
cd ui
npm ci --ignore-scripts
npm run build
cd ..
.venv\Scripts\python.exe -m app.portal_launcher
```

`python tools/build_launcher.py` 生成 dist/Trading Bot.exe，再复制到项目根目录。exe 是薄启动器，分发时保留源码、配置、.venv 和 ui/dist。旧 `python -m app.launcher` 入口也打开同一个 Portal；不再启动另一套 Tk 工作流。

测试：`python -m unittest discover -s tests -v`，前端状态判断可运行 `node --test ui/src/runtime.test.js`。Portal 测试在隔离目录动态登记新模块、回放本地合成账户并验证切换边界，不发送券商订单。exe 的 `--self-test <report.json>` 仅验证本地 HTTP 就绪。

## 共用操作入口

先打开工作台，再使用同一个账户和数据选择：

```text
python bot.py workbench view
python bot.py workbench status
python bot.py workbench refresh
python bot.py workbench preview
python bot.py workbench prepare
python bot.py workbench orders
```

操作返回任务编号，进度和结果仍在 Portal 的运行记录中。`view` 包含订单预览和具体执行阻碍；准备不会下单。确认订单后，执行命令必须带 `--confirm-broker <当前模拟券商编号>`。请求超时不自动重发。服务器可加 `--url https://你的工作台地址`，使用与网页相同的工作台登录；密码交互输入，不存入命令行。

新增插件加载失败会在列表显示不可用和原因；所选插件失效时禁止交易，账户查询和修复操作尽量保持可用。`/api/state` 的 `actions` 是按钮状态的唯一业务来源，拒绝操作时返回同一原因。默认不扩展页面或新增券商品牌分支。
