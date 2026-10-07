---
name: "Trading Bot · 个人交易工作台"
description: "白色、中性色与 indigo 的中文列表工作台"
colors:
  ink: "#202631"
  secondary: "#596272"
  border: "#e1e5eb"
  border-strong: "#b8c1ce"
  surface: "#ffffff"
  surface-inset: "#f5f7fa"
  sidebar: "#f7f8fa"
  accent: "#454dc3"
  accent-hover: "#363da7"
  accent-soft: "#eeeffb"
  success: "#28704c"
  success-soft: "#eff8f2"
  warning: "#8b560e"
  warning-soft: "#fff7e8"
  danger: "#ae343b"
  danger-soft: "#fff2f2"
  control-ink: "#374050"
  field-ink: "#424c5b"
  disabled-ink: "#636d7b"
  tag-neutral-ink: "#566171"
  tag-neutral-surface: "#eef1f5"
  tag-selected-ink: "#3d449e"
  nav-selected-ink: "#3c449f"
  nav-selected-surface: "#e9ebf9"
typography:
  headline:
    fontFamily: "-apple-system, BlinkMacSystemFont, \"Segoe UI\", \"PingFang SC\", \"Microsoft YaHei\", sans-serif"
    fontSize: "26px"
    fontWeight: 600
    lineHeight: 1.45
    letterSpacing: "-.025em"
  headline-mobile:
    fontFamily: "-apple-system, BlinkMacSystemFont, \"Segoe UI\", \"PingFang SC\", \"Microsoft YaHei\", sans-serif"
    fontSize: "24px"
    fontWeight: 600
    lineHeight: 1.45
    letterSpacing: "-.025em"
  title:
    fontFamily: "-apple-system, BlinkMacSystemFont, \"Segoe UI\", \"PingFang SC\", \"Microsoft YaHei\", sans-serif"
    fontSize: "18px"
    fontWeight: 600
    lineHeight: 1.45
    letterSpacing: "-.012em"
  instance-title:
    fontFamily: "-apple-system, BlinkMacSystemFont, \"Segoe UI\", \"PingFang SC\", \"Microsoft YaHei\", sans-serif"
    fontSize: "22px"
    fontWeight: 600
    lineHeight: 1.45
    letterSpacing: "-.012em"
  body:
    fontFamily: "-apple-system, BlinkMacSystemFont, \"Segoe UI\", \"PingFang SC\", \"Microsoft YaHei\", sans-serif"
    fontSize: "14px"
    fontWeight: 400
    lineHeight: 1.6
  label:
    fontFamily: "-apple-system, BlinkMacSystemFont, \"Segoe UI\", \"PingFang SC\", \"Microsoft YaHei\", sans-serif"
    fontSize: "13px"
    fontWeight: 500
    lineHeight: 1.6
  button:
    fontFamily: "-apple-system, BlinkMacSystemFont, \"Segoe UI\", \"PingFang SC\", \"Microsoft YaHei\", sans-serif"
    fontSize: "13px"
    fontWeight: 500
    lineHeight: 1.5
  metadata:
    fontFamily: "-apple-system, BlinkMacSystemFont, \"Segoe UI\", \"PingFang SC\", \"Microsoft YaHei\", sans-serif"
    fontSize: "12px"
    fontWeight: 400
    lineHeight: 1.65
  code:
    fontFamily: "\"Cascadia Code\", Consolas, \"Liberation Mono\", monospace"
    fontSize: "12px"
    fontWeight: 400
    lineHeight: 1.7
rounded:
  flat: "0px"
  tag: "4px"
  control: "6px"
  floating: "10px"
spacing:
  xs: "8px"
  sm: "12px"
  md: "16px"
  lg: "20px"
  section: "24px"
  gutter: "36px"
components:
  button-primary:
    backgroundColor: "{colors.accent}"
    textColor: "{colors.surface}"
    typography: "{typography.button}"
    rounded: "{rounded.control}"
    padding: "7px 13px"
  button-primary-hover:
    backgroundColor: "{colors.accent-hover}"
  button-secondary:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.control-ink}"
    typography: "{typography.button}"
    rounded: "{rounded.control}"
    padding: "7px 13px"
  button-secondary-hover:
    backgroundColor: "{colors.surface-inset}"
    textColor: "{colors.ink}"
  button-disabled:
    backgroundColor: "#f2f4f7"
    textColor: "{colors.disabled-ink}"
  button-primary-disabled:
    backgroundColor: "#e6e8f4"
    textColor: "#62698a"
  input:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.ink}"
    rounded: "{rounded.control}"
    padding: "8px 10px"
  nav-selected:
    backgroundColor: "{colors.nav-selected-surface}"
    textColor: "{colors.nav-selected-ink}"
    rounded: "{rounded.control}"
    padding: "10px 12px"
  tag-neutral:
    backgroundColor: "{colors.tag-neutral-surface}"
    textColor: "{colors.tag-neutral-ink}"
    rounded: "{rounded.tag}"
    padding: "3px 8px"
  tag-pending:
    backgroundColor: "{colors.warning-soft}"
    textColor: "{colors.warning}"
    rounded: "{rounded.tag}"
    padding: "3px 8px"
  content-section:
    backgroundColor: "transparent"
    textColor: "{colors.ink}"
    rounded: "{rounded.flat}"
    padding: "26px 0"
  dialog:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.ink}"
    rounded: "{rounded.floating}"
    padding: "28px"
---

# Design System: Trading Bot · 个人交易工作台

## Overview

**Creative North Star: "The Personal Workbench"**

白色主内容与浅中性侧栏构成清楚的个人工作台。日常信息沿行与细分隔线展开，indigo 连接当前任务、选择和键盘焦点；标题、正文和辅助信息以克制的字级区分。

详情、配置和维护按需进入，列表保留识别运行所需的名称、账户、状态和时间。模块身份与事实来源相邻；未知值和待核对状态用明确文字呈现，视觉层级不替代产品状态。

**Key Characteristics:**

- 白色内容底、浅中性侧栏与平面分隔列表。
- indigo 主要动作、链接、选中状态与键盘焦点。
- 中文系统字体与紧凑但可辨的文字层级。
- 来源、时区、状态和阻塞原因贴近对应内容。

本规范更新了已获授权的重构系统。源码依据为 [令牌](ui/src/styles/tokens.css)、[基础控件](ui/src/styles/base.css)、[应用壳](ui/src/styles/shell.css)、[功能样式](ui/src/styles/features.css) 与 [共享组件](ui/src/ui.jsx)。产品边界见 [PRODUCT.md](PRODUCT.md)；导航及页面关系见 [navigation](ui/src/app/navigation.js) 和 [页面回归](ui/src/app/pages.test.js)。

## Colors

白色内容、浅中性导航面与灰阶文字承载日常信息；indigo 是唯一的动作与选择主色。前置令牌记录实际值，组件变体保留源码中的局部颜色。

### Primary

- **Workbench Indigo** (`accent` / `accent-hover` / `accent-soft`)：主要按钮、链接、当前标签页、输入光标、复选框与焦点轮廓。
- **Selected Navigation** (`nav-selected-ink` / `nav-selected-surface`)：当前主导航行；**Selected Context** (`tag-selected-ink`)：上下文标签。

### Neutral

- **Clear Surface / Inset / Sidebar** (`surface` / `surface-inset` / `sidebar`)：主内容、内嵌读数与应用导航。
- **Text Inks** (`ink` / `secondary` / `control-ink` / `field-ink`)：正文、辅助信息、次要动作和字段标签。
- **Quiet Edges** (`border` / `border-strong`)：内容分隔线与控件边界。
- **Neutral State** (`tag-neutral-ink` / `tag-neutral-surface`)：普通等待和未强调的状态；`disabled-ink` 保持不可用操作可读。
- **Verified Green / Unresolved Amber / Error Red**：`success`、`warning`、`danger` 及各自的浅色面。净资产 K 线使用绿涨、红跌、灰平的同一语义色。

**The Single Accent Rule.** indigo 统一主要动作与选择；绿色、琥珀和红色只表达对应的语义状态。普通运行等待保持中性。

## Typography

**Display / Body Font:** 同一系统无衬线栈，中文回退按 PingFang SC、Microsoft YaHei 排列。
**Code Font:** Cascadia Code、Consolas、Liberation Mono，回退 monospace。

页面标题使用 `headline`，手机使用 `headline-mobile`；分区标题使用 `title`，实例详情名使用 `instance-title`，手机详情名为 (20px)。正文使用 `body`；常规说明、动作和列表数值多为 (13px)，辅助信息使用 `metadata`，窄屏列表附注可为 (11px)。实例资金比例是详情中的普通事实，不继承上轮的大号数字强调。说明正文通常限于 (72–75ch)，README 约为 (78ch)。

**The Adjacent Evidence Rule.** 事实、来源、时间与时区保持同组；数字和时间使用等宽数字特性，未知数据不被装饰性指标补齐。

当前字体与版式来自已披露的 fallback 实现。引擎流程保留 batch label 失败（包括两次 concept seed 尝试及 detect）；字体目录与字体评审未验证。本文不宣称引擎门槛、自动检测或字体目录审核完成。

## Layout

桌面应用壳为粘性左导航 (208px) 加流式工作区；内容最大宽度为 (1248px)，常规主内容内边距为 (34px 36px 64px)。在 (1100px) 水平留白收为 (28px)；在 (681–850px) 导航为 (184px)，内容左右留白为 (24px)。手机 (680px 及以下) 导航在顶部用三列两行完整显示六个入口，主内容内边距为 (26px 20px 48px)。

当前主导航为 总览 (`overview`)、策略运行 (`runs`)、账户 (`account`)、模块配置 (`modules`)、研究 (`research`)、系统 (`system`)；子视图由带下边线的标签页区分。日常运行列表与详情路由分离，不把当前手动上下文当成所有实例的绑定。

表单常规三列，在 (1000px) 变两列，手机变一列。运行行在 (1150px) 调整状态位置，在手机重排为身份 / 状态、事实 / 详情入口；事实组在手机纵排。表格只在自身容器横向滚动，长名称与状态说明允许换行。

**The List First Rule.** 日常内容用行、列与分隔线组织；详情和表单按需展开，不为每项运行增加独立的悬浮卡片。

## Elevation & Depth

列表、分区和读数保持平面，依靠细线、留白与浅色内嵌面区分层级。原生确认对话框使用遮罩与环境阴影 (`0 18px 60px rgba(20, 28, 43, .18)`)，仅供弹层。按钮颜色状态过渡为 (160ms ease)，后台开关滑块为 (180ms ease)；降低动态偏好压至 (.01ms)，动画只执行一次。

**The Content Is Flat Rule.** 主内容保持平面；仅对话框使用环境阴影与遮罩，不把弹层的深度复制到日常列表。

## Shapes

日常内容以直边分隔，不采用独立圆角面板外壳。状态标签使用 `tag` 圆角，输入和按钮使用 `control` 圆角，对话框与登录面使用 `floating` 圆角；标签页为直边。边界通常为 (1px)，选中标签页下边线为 (2px)。导航图标使用一致、无底砖的描边 SVG；圆点、开关与检查进度保留功能所需的形状。

## Components

### Buttons

indigo 主要动作与白底描边次要动作共享按钮字级。桌面最小高度为 (36px)，手机为 (40px)、内边距 (8px 12px)；禁用主按钮与次按钮各有浅色处理，原有阻塞原因继续可见。文字链接默认无下划线，悬停出现下划线。焦点为 (2px) indigo 轮廓，常规偏移 (3px)。

### Chips

紧凑状态标签使用 (12px)、字重 (500)。普通运行等待用中性面，待核对用琥珀；成功、错误与选中上下文使用对应语义色。状态文字来自既有 helper，颜色不创造新状态。

### Cards / Containers

共享内容分区保持透明、无圆角、无阴影，常规上下内边距为 (26px)，手机为 (24px)，相邻内容以底部细线分开。内嵌结果与代码可用浅色面；登录面和对话框保留它们的独立外壳。

### Inputs / Fields

字段标签与输入纵向相邻，标签字级使用 `label`。输入为白底、较强边界与控件圆角，最小高度 (38px)、字号 (14px)、内边距 (8px 10px)。悬停增强边界，焦点使用统一 indigo 轮廓，`aria-invalid` 使用错误色。禁用字段保留完整对比与中性底。

### Navigation

桌面主导航为图标与文字行；当前行使用选中导航色及字重 (600)。手机六项入口三列两行，每项最小高度 (40px)，间隙 (4px)。二级标签页以 indigo 文字与下边线定位，焦点偏移为 (-3px)。保留跳至内容入口与后台、实例、单策略计划的独立状态入口。

### Confirmation Dialogs

[共享 Dialog](ui/src/components/Dialog.jsx) 以原生 `showModal` 打开，背景隔离、焦点限制在对话框内，关闭后恢复触发焦点。Escape 与遮罩点击遵守调用方的取消权限，不能绕过正在提交时的锁。最大宽度为 (540px)，桌面左右保留 (24px)，手机保留 (16px)；确认文字与真实账户和操作范围保持相邻。

### Account Candles

净资产 K 线属于账户内容；周期选择与当前观测读数相邻，形成中的柱体使用降低透明度 (0.72)。缺失时段不补齐，单点显示横线；时间周期明确标注美东时间。图表保留键盘选择与数值表入口，读数来源约束见 [candle tests](ui/src/account_candles.test.js)。

## Do's and Don'ts

### Do:

- **Do** 沿四层样式扩展，共享令牌和基础控件，不恢复上轮页面皮肤。
- **Do** 保持模块身份、状态、来源、时间及不可用原因在对应内容旁。
- **Do** 用真实状态 helper 与账户口径驱动标签、数字和可用操作。
- **Do** 让手机导航、列表和表单按可用宽度重排，宽表格在自身容器内滚动。
- **Do** 保持实例、单策略计划与交易后台的状态和操作边界可辨。

### Don't:

- **Don't** 把未知持仓、现金或执行时间显示成零或已确认状态。
- **Don't** 将实例资金比例换算为推测的金额，或将净资产 K 线描述为已核实收益。
- **Don't** 用营销标题、装饰指标、厚重面板或无意义动画增加视觉噪音。
- **Don't** 让布局更改隐藏有效运行或改变现有交易确认与守卫。
- **Don't** 宣称 fallback 已通过 Impeccable 引擎门槛、自动检测或字体目录审核。
