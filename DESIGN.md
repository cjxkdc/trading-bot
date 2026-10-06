---
name: "Trading Bot · 个人交易工作台"
description: "暖中性、平面、中文优先的个人交易工作台"
colors:
  canvas: "#f6f6f4"
  surface: "#ffffff"
  surface-inset: "#f2f1ee"
  ink: "#1d1f23"
  ink-hover: "#36383e"
  secondary: "#545b66"
  muted: "#656b75"
  accent: "#5740c7"
  accent-soft: "#eee9fb"
  border: "#e6e4e0"
  border-strong: "#c4c1bc"
  success: "#28734f"
  success-soft: "#edf5ef"
  warning: "#8a5300"
  warning-soft: "#fff4e0"
  danger: "#a33432"
  danger-soft: "#fff0ee"
typography:
  headline:
    fontFamily: '-apple-system, BlinkMacSystemFont, "Segoe UI", "Microsoft YaHei UI", "Microsoft YaHei", "PingFang SC", sans-serif'
    fontSize: "34px"
    fontWeight: 650
    lineHeight: 1.3
    letterSpacing: "-.025em"
  headline-mobile:
    fontFamily: '-apple-system, BlinkMacSystemFont, "Segoe UI", "Microsoft YaHei UI", "Microsoft YaHei", "PingFang SC", sans-serif'
    fontSize: "27px"
    fontWeight: 650
    lineHeight: 1.3
    letterSpacing: "-.025em"
  title:
    fontFamily: '-apple-system, BlinkMacSystemFont, "Segoe UI", "Microsoft YaHei UI", "Microsoft YaHei", "PingFang SC", sans-serif'
    fontSize: "19px"
    fontWeight: 650
    lineHeight: 1.45
  instance-title:
    fontFamily: '-apple-system, BlinkMacSystemFont, "Segoe UI", "Microsoft YaHei UI", "Microsoft YaHei", "PingFang SC", sans-serif'
    fontSize: "22px"
    fontWeight: 650
    lineHeight: 1.4
  body:
    fontFamily: '-apple-system, BlinkMacSystemFont, "Segoe UI", "Microsoft YaHei UI", "Microsoft YaHei", "PingFang SC", sans-serif'
    fontSize: "15px"
    fontWeight: 400
    lineHeight: 1.65
  label:
    fontFamily: '-apple-system, BlinkMacSystemFont, "Segoe UI", "Microsoft YaHei UI", "Microsoft YaHei", "PingFang SC", sans-serif'
    fontSize: "14px"
    fontWeight: 400
    lineHeight: 1.65
  button:
    fontFamily: '-apple-system, BlinkMacSystemFont, "Segoe UI", "Microsoft YaHei UI", "Microsoft YaHei", "PingFang SC", sans-serif'
    fontSize: "14px"
    fontWeight: 500
    lineHeight: 1.4
  fact:
    fontFamily: '-apple-system, BlinkMacSystemFont, "Segoe UI", "Microsoft YaHei UI", "Microsoft YaHei", "PingFang SC", sans-serif'
    fontSize: "24px"
    fontWeight: 650
    lineHeight: 1.3
  code:
    fontFamily: "Consolas, monospace"
    fontSize: "14px"
    fontWeight: 400
    lineHeight: 1.6
rounded:
  tag: "6px"
  control: "8px"
  panel: "12px"
spacing:
  xs: "8px"
  sm: "12px"
  md: "16px"
  lg: "20px"
  panel: "24px"
  gutter: "32px"
components:
  button-primary:
    backgroundColor: "{colors.ink}"
    textColor: "{colors.surface}"
    typography: "{typography.button}"
    rounded: "{rounded.control}"
    padding: "10px 16px"
  button-primary-hover:
    backgroundColor: "{colors.ink-hover}"
    textColor: "{colors.surface}"
  button-secondary:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.ink}"
    typography: "{typography.button}"
    rounded: "{rounded.control}"
    padding: "10px 16px"
  button-secondary-hover:
    backgroundColor: "{colors.surface-inset}"
  button-disabled:
    backgroundColor: "{colors.surface-inset}"
    textColor: "{colors.secondary}"
  input:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.ink}"
    typography: "{typography.body}"
    rounded: "{rounded.control}"
    padding: "9px 12px"
  nav-selected:
    backgroundColor: "{colors.accent-soft}"
    textColor: "{colors.accent}"
    rounded: "{rounded.control}"
    padding: "12px"
  tag-neutral:
    backgroundColor: "{colors.surface-inset}"
    textColor: "{colors.secondary}"
    rounded: "{rounded.tag}"
    padding: "4px 9px"
  tag-pending:
    backgroundColor: "{colors.warning-soft}"
    textColor: "{colors.warning}"
    rounded: "{rounded.tag}"
    padding: "4px 9px"
  panel:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.ink}"
    rounded: "{rounded.panel}"
    padding: "24px"
---

# Design System: Trading Bot · 个人交易工作台

## Overview

**Creative North Star: "The Personal Workbench"**

暖白底、白色内容面与墨黑动作构成安静、清楚的个人工作台。中文阅读、事实层级与操作可辨性先于装饰；紫色为导航选择、链接与当前上下文提供位置线索。

页面沿用现有工作结构。模块身份、事实来源和时间紧邻内容；未知值与待核对状态保留明确文字，颜色只辅助辨识。平面边界与间距组织内容，不模拟纸张或物理器具。

**Key Characteristics:**

- 暖中性底色与平面白色内容面。
- 墨黑主要动作，少量紫色选择与链接。
- 中文系统字体、清楚的数字层级。
- 来源、时间、状态与操作原因相邻。

本规范从 [全局样式](ui/src/style.css)、[实例样式](ui/src/strategy_runs.css)、[页面组件](ui/src/main.jsx) 与 [策略运行组件](ui/src/components/StrategyRuns.jsx) 提取。前置方向中的页标题 (24px) 未落地；实际标题层级见前置令牌。行为边界见 [PRODUCT.md](PRODUCT.md)，状态与资金比例例证见 [runtime](ui/src/runtime.test.js)、[automation](ui/src/automation.test.js)、[strategy instances](ui/src/strategy_instances.test.js) 和 [preflight](ui/src/preflight.test.js) 测试。

## Colors

暖灰中性面承载主要信息，紫色承担选择与链接，语义状态色保持克制。前置令牌为唯一颜色值来源。

### Primary

- **Workbench Violet** (`accent` / `accent-soft`)：当前导航、选中策略、链接、图表线与比例条。
- **Action Ink** (`ink` / `ink-hover`)：正文和主要动作；主要按钮使用白字。

### Neutral

- **Warm Canvas** (`canvas`)：应用底与导航背景。
- **Clear Surface** (`surface`)：面板、表单与页眉。
- **Warm Inset** (`surface-inset`)：禁用控件、普通标签及内嵌信息。
- **Secondary Ink** (`secondary`)：说明、字段标签和来源时间；**Muted Ink** (`muted`)：更轻的辅助标记。
- **Quiet Edge** (`border`)：分隔线和面板边界；**Control Edge** (`border-strong`)：输入框与次要按钮。
- **Verified Green / Unresolved Amber / Error Red**：分别对应 `success`、`warning`、`danger` 及其浅色面。绿色标签目前使用中性内嵌面；不要求所有成功信息使用绿色底。

**The Action Anchor Rule.** 墨黑用于当前主要动作；紫色用于位置、选择与链接，不把普通等待状态变成强调色。

**The State Truth Rule.** 状态文字来自现有 helper；普通实例等待用中性标签，未完成批次用暖琥珀，成功与错误用各自的语义色。

## Typography

**Display / Body Font:** 同一系统无衬线栈，中文回退为 Microsoft YaHei UI、Microsoft YaHei 和 PingFang SC。
**Code Font:** Consolas，回退 monospace；数字对齐依靠 `tabular-nums`，不将整个界面改成等宽字体。

页面标题采用 `headline`，窄屏 (680px 及以下) 使用 `headline-mobile`；普通分区采用 `title`，实例身份采用 `instance-title`，实例资金比例采用 `fact`。正文采用 `body`，段落行高为 (1.75)；字段说明与常规动作分别采用 `label` 与 `button`。实例动作字号为 (15px)，辅助信息多为 (13–14px)，紧凑账户比例说明为 (12px)。

**The Adjacent Evidence Rule.** 让来源、检查时间、时区与所说明的事实保持同组；数字使用等宽数字特性，未知值保留破折号或明确的未知文案。

字体是已批准并实际落地的系统字形选择。构建引擎的字体目录 / 排名仍因浏览器与字体目录环境不可用而未完成；本文不宣称该门槛或后续引擎阶段已通过。

## Layout

桌面采用固定左导航 (240px) 与流式工作区；内容最大宽度为 (1480px)，常规左右留白采用 `gutter`。导航在 (1170px) 收至 (208px)，在 (960px) 收至 (180px)；首页在 (960px) 转为单列。在 (680px) 导航转为顶部可横向滚动的粘性行，主内容留白为 (24px 16px)。

常用间距取前置 spacing 令牌；不强行统一任务特有的局部尺寸。动作行允许换行。实例事实桌面三列，在 (760px) 纵排且去掉列分隔线。表格和宽图表在自身容器中滚动，长模块名与状态说明可折行。账户比例概要依次为账户标题、细比例条、左右对齐的已预留 / 可分配比例，再接简短计数说明。

## Elevation & Depth

面板保持平面；用底色、细边界与留白表达层级，不添加装饰性阴影或渐变。 对话框以遮罩和细边界分离，面板与按钮不使用阴影。状态变化采用轻量颜色过渡；按钮 (150ms ease-out)，后台开关 (160ms ease)。降低动态偏好将动画和过渡压至 (.01ms)。

**The Flat Surface Rule.** 面板保持平面；用底色、细边界与留白表达层级，不添加装饰性阴影或渐变。

## Shapes

内容面使用 `panel` 圆角，输入和动作使用 `control` 圆角，状态标签使用 `tag` 圆角。主要边界为 (1px)；比例条细而圆润。图标直接采用一致的描边 SVG，不包在装饰性方砖中。圆点、加载指示与开关保留其功能所需的圆形。

## Components

### Buttons

黑底白字主要动作与白底描边次要动作共享控件圆角和常规按钮令牌，标准最小高度为 (42px)。实例动作使用 (15px)、内边距 (8px 16px)。禁用状态以中性内嵌面和次要文字保持可读，旁边保留原因。等待中的已启用实例以黑色停止动作作为锚点；有待核对批次时黑色锚点为核对动作，停止仍按现有守卫显示为描边动作。

### Chips

中性标签描述普通等待与数量；琥珀描述未完成批次。紫色描述选中上下文，错误使用红色。标签不是新状态的来源。

### Cards / Containers

白色、细边界、平面圆角面板。普通内边距采用 `panel`；实例面板为 (18px 24px)，账户概要为 (14px 24px)。窄屏侧边内边距为 (16px)。详情区以分隔线进入，不继续叠加同样的卡片外壳。

### Inputs / Fields

白底、较强边界、控件圆角，最小高度为 (42px)；监控区域的紧凑字段为 (40px)。字段标签与输入纵向相邻，禁用状态使用中性内嵌面。复选框与输入光标使用紫色。当前键盘焦点使用浅色边框轮廓，与批准方向的紫色焦点存在偏差；这项偏差不作为后续界面的规范继承，sidecar 示例仅保留源码已有的焦点偏移。

### Navigation

描边图标与中文标签并列；选中行浅紫底、紫色文字、稍强字重，悬停为中性内嵌面。窄屏保留文字并横向滚动；页名和页面 id 由既有产品结构决定。

### Account Allocation

账户比例条高度为 (4px)，其下数字为 (20px)、字重 (600)；实例资金比例使用更强的 `fact` 层级。比例仍是账户净值的上限，不把视觉条长或比例换算成资产金额。模块名称、计划的美东时间、下一次执行的本地时间继续显式显示。

## Do's and Don'ts

### Do:

- **Do** 保持事实、模块身份、来源、时间和阻塞原因在对应内容旁。
- **Do** 使用源码中的状态 helper、资金比例口径与确认流程；视觉样式不扩大交易权限。
- **Do** 让窄屏操作换行、事实纵排，表格在自身容器中横向滚动。
- **Do** 保留独立的交易后台、旧自动计划和策略实例状态入口。

### Don't:

- **Don't** 将未知值显示为零，或把资金比例换算成推测的资产金额。
- **Don't** 用紫色强调普通实例等待或实例数量。
- **Don't** 添加装饰图标底砖、面板阴影或拟物材质。
- **Don't** 把当前浅色焦点边框或未完成的字体排名当作已经验证的系统规范。
