---
name: "Trading Bot · 个人交易工作台"
description: "浅色为主的中文工作台（「青石」配色），可切换中性深色；靛蓝为唯一动作色"
colors:
  bg: "#f4f4f1"
  sidebar: "#fafaf8"
  surface: "#ffffff"
  surface-raised: "#f7f7f4"
  surface-inset: "#efefeb"
  surface-hover: "#f3f3ef"
  ink: "#1a1a18"
  body-ink: "#33332f"
  secondary: "#5b5b56"
  faint: "#8a8a84"
  border: "#e4e4df"
  border-strong: "#cfcfc8"
  accent: "#2451c7"
  accent-hover: "#1c43a6"
  accent-ink: "#ffffff"
  accent-soft: "#e9eefb"
  info: "#2451c7"
  success: "#1a7f4b"
  warning: "#9a5b00"
  danger: "#c0392b"
  gain: "#1a7f4b"
  loss: "#c0392b"
colors-dark:
  bg: "#121315"
  sidebar: "#17181b"
  surface: "#1c1d21"
  surface-raised: "#232428"
  ink: "#ecece8"
  secondary: "#a3a39e"
  faint: "#74746f"
  border: "#2b2c31"
  accent: "#7e9dff"
  accent-ink: "#0b1430"
  success: "#4cc38a"
  warning: "#e6b254"
  danger: "#f07167"
typography:
  page-title:
    fontFamily: "\"Segoe UI Variable Text\", \"Segoe UI\", -apple-system, \"PingFang SC\", \"Hiragino Sans GB\", \"Microsoft YaHei UI\", \"Microsoft YaHei\", system-ui, sans-serif"
    fontSize: "24px"
    fontWeight: 600
    letterSpacing: "-.01em"
  section-title:
    fontSize: "18px"
    fontWeight: 600
  card-title:
    fontSize: "15px"
    fontWeight: 600
  body:
    fontSize: "14px"
    fontWeight: 400
    lineHeight: 1.5
  caption:
    fontSize: "13px"
    color: "{colors.secondary}"
  label:
    fontSize: "13px"
    fontWeight: 500
  column-header:
    fontSize: "12.5px"
    fontWeight: 500
    color: "{colors.faint}"
  numeric:
    fontVariantNumeric: "tabular-nums"
  code:
    fontFamily: "\"Cascadia Mono\", \"SF Mono\", \"JetBrains Mono\", Consolas, monospace"
    fontSize: "13px"
rounded:
  control: "8px"
  card: "12px"
  tag: "999px"
  dialog: "14px"
spacing:
  table-cell: "10px 16px"
  card: "20px 24px"
  gap: "16px"
  page: "12px 40px 56px"
components:
  button-primary:
    backgroundColor: "{colors.accent}"
    textColor: "{colors.accent-ink}"
    minHeight: "36px"
    padding: "6px 14px"
  button-secondary:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.ink}"
    border: "{colors.border-strong}"
  button-danger:
    backgroundColor: "danger-soft"
    textColor: "{colors.danger}"
  card:
    backgroundColor: "{colors.surface}"
    border: "{colors.border}"
    radius: "12px"
    shadow: "0 1px 2px rgba(26, 26, 24, .04)"
  status-tag:
    shape: "pill"
    dot: "6px currentColor"
  nav-selected:
    backgroundColor: "{colors.surface-inset}"
    textColor: "{colors.accent}"
---

# Design System: Trading Bot · 个人交易工作台

## Overview

界面是一个安静的浅色工作台：浅暖灰底、白色面板、很淡的边线，靠留白和层级区分内容，不靠阴影和色块。深色主题是中性炭灰，供夜间或偏好使用，顶栏可切换，选择保存在当前浏览器中。

组织原则是「一个分区只做一件事」：左侧七个分区（现在、策略、回测、运行、账户、连接、系统），每页标题下一句话说明这页能做什么，右上角至多一个主按钮，次要操作降为文字链接或折叠。市场状态、时区和后台开关在顶栏；模拟或实盘标识属于具体账户及运行，不作为整个工作台的固定标签。

源码依据为 [令牌](ui/src/styles/tokens.css)、[基础控件](ui/src/styles/base.css)、[应用壳](ui/src/styles/shell.css)、[功能样式](ui/src/styles/features.css)、[账户与策略样式](ui/src/styles/account-strategy.css) 与 [共享组件](ui/src/ui.jsx)。产品边界见 [PRODUCT.md](PRODUCT.md)。

## Colors

所有颜色均定义为 `:root` 上的令牌，样式文件不直接写十六进制颜色。浅色为默认值，`[data-theme='dark']` 重新定义同名令牌。

- **动作色** `accent`：靛蓝，只用于主要按钮、当前导航、文字链接、输入焦点和光标。主要按钮上的文字使用 `accent-ink`。每页至多一个主要按钮。
- **状态色** `success` / `warning` / `danger`：分别表示运行中或已核对、待处理、错误，各有对应的 `-soft` 底色；标签带圆点并一定配文字，不靠颜色单独表意。
- **涨跌色** `gain` / `loss`：只用于盈亏、K 线与买卖方向，与状态色分开定义。默认绿涨红跌；顶栏选择「红涨」后，`[data-updown='cn']` 将两者对调，状态色不受影响。
- **中性层级** `bg` → `sidebar` → `surface` → `surface-raised`：页面底、侧栏、面板与面板内的次级区域；`surface-inset` 用于当前导航项、折叠区和代码区，`surface-hover` 用于行悬停。

## Typography

正文使用系统无衬线字体（Windows 上为 Segoe UI 加微软雅黑，macOS 上为 PingFang），字号 14px，行高 1.5。页面标题 24px，区块标题 18px，卡片标题 15px，说明文字和字段标签 13px，表头 12.5px 不转大写。

数字一律启用 `tabular-nums` 使列内对齐，但不再整段使用等宽字体；等宽字体只用于代码、编辑器和记录编号。

## Layout

桌面为固定左侧导航（232px）加工作区，导航按「工作 / 资产 / 设置」分组，当前项为浅灰底加动作色文字，「现在」上的角标表示需要处理的事项数。内容左对齐，最大宽度 1200px，左右留白 40px；顶栏只放时钟、市场状态、涨跌与主题切换、后台开关。

每页由 `PageHeader` 开始：标题、一句说明、右侧主按钮；子页面（运行详情、新建运行、手动调仓）在标题上方显示「返回」。有多个分页的分区（账户、连接、系统）在标题下用下划线式分页；只有一个分页时不显示分页条。

内容以卡片组织：`.card` 为白底、细边线、12px 圆角的面板，不再有灰色标题条；列表用行分隔线而不是嵌套卡片。850px 以下侧栏变窄，680px 及以下导航改为顶部网格，卡片与表单改为单列，页面不出现横向滚动。

## Elevation & Shapes

面板依靠边线和底色分层，浅色主题下卡片只有 1px 的极浅投影，深色主题没有投影。确认对话框使用 14px 圆角与半透明遮罩，标题和按钮区不再用色带。控件圆角 8px，卡片 12px，状态标签为胶囊形。

## Components

### Buttons

最小高度 36px（手机 40px），小号 32px。主要按钮为靛蓝底白字；次要按钮为白底加 `border-strong` 边线；`danger` 变体用于「关闭已撤单/拒单」等需要谨慎确认的操作；文字链接用于导航类的次要动作。

### Tables

单元格内边距 10px 16px，表头浅灰文字、不转大写，行悬停加亮。订单表的状态列使用胶囊标签：已成交为绿色，等待成交为蓝色，部分成交和待核对为琥珀色，已撤单和已拒绝为红色，已结束和已关闭为中性色。买卖方向使用涨跌色。

### Confirmation Dialogs

[共享 Dialog](ui/src/components/Dialog.jsx) 以原生 `showModal` 打开。提交订单前显示委托笔数、绑定账户环境，以及按当前账户币种计算的买入和卖出估算金额；订单数在提交时才计算的模块显示目标标的数量和说明。「关闭已撤单/拒单」与「确认未发送并关闭」要求勾选「已在券商端核对」后才能确认。

### Status Bar Preferences

顶栏的「绿涨 / 红涨」与「浅色 / 深色」两组切换按钮写入 `localStorage`。存储不可用时使用默认值（绿涨、浅色），页面照常显示。

## Do's and Don'ts

### Do:

- **Do** 新增颜色时先在 `tokens.css` 中为浅色和深色两套主题都定义令牌。
- **Do** 数字启用 `tabular-nums`；未知值显示「—」，不显示为零。
- **Do** 盈亏与买卖方向使用 `gain` / `loss`，系统状态使用 `success` / `warning` / `danger`。
- **Do** 每页只有一个主要按钮，次要操作降级为文字链接或折叠。
- **Do** 保持模块身份、状态、来源、时间及不可用原因与对应内容相邻。

### Don't:

- **Don't** 在样式或组件中直接写十六进制颜色，否则主题切换会失效。
- **Don't** 用颜色单独表达状态；标签与提示必须同时给出文字。
- **Don't** 在同一页并列多个同等重要的入口；同一件事只保留一个主入口。
- **Don't** 让版式改动隐藏有效运行，或改变现有的交易确认与守卫。
