---
name: "Trading Bot · 个人交易工作台"
description: "深色交易终端风格的中文工作台，可切换浅色；橙色为唯一动作色"
colors:
  bg: "#0b0e14"
  sidebar: "#0e1219"
  surface: "#121721"
  surface-raised: "#182030"
  surface-inset: "#0f141c"
  surface-hover: "#1b2333"
  ink: "#e4e8f0"
  body-ink: "#c3cad7"
  secondary: "#8b95a8"
  faint: "#5f6a7e"
  border: "#222a38"
  border-strong: "#323c4e"
  accent: "#f5a623"
  accent-hover: "#ffb845"
  accent-ink: "#1b1204"
  info: "#5aa2ff"
  success: "#2ebd85"
  warning: "#e9c15b"
  danger: "#f6465d"
  gain: "#2ebd85"
  loss: "#f6465d"
colors-light:
  bg: "#eef1f5"
  surface: "#ffffff"
  surface-raised: "#f5f7fa"
  ink: "#18202e"
  secondary: "#5a6476"
  border: "#dde2ea"
  accent: "#e8890c"
  info: "#2a6fdb"
  success: "#12825a"
  warning: "#9a6200"
  danger: "#d02f46"
typography:
  page-title:
    fontFamily: "-apple-system, BlinkMacSystemFont, \"Segoe UI\", \"PingFang SC\", \"Microsoft YaHei\", sans-serif"
    fontSize: "16px"
    fontWeight: 600
  panel-title:
    fontFamily: "-apple-system, BlinkMacSystemFont, \"Segoe UI\", \"PingFang SC\", \"Microsoft YaHei\", sans-serif"
    fontSize: "13px"
    fontWeight: 600
  body:
    fontFamily: "-apple-system, BlinkMacSystemFont, \"Segoe UI\", \"PingFang SC\", \"Microsoft YaHei\", sans-serif"
    fontSize: "13px"
    fontWeight: 400
    lineHeight: 1.55
  label:
    fontSize: "11px"
    fontWeight: 500
    letterSpacing: ".02em"
  column-header:
    fontSize: "10.5px"
    fontWeight: 600
    letterSpacing: ".06em"
    textTransform: "uppercase"
  numeric:
    fontFamily: "\"Cascadia Mono\", \"SF Mono\", \"JetBrains Mono\", Consolas, \"Liberation Mono\", monospace"
    fontVariantNumeric: "tabular-nums"
rounded:
  control: "4px"
  tag: "3px"
  dialog: "6px"
spacing:
  table-cell: "7px 12px"
  panel: "14px 16px"
  gap: "12px"
  gutter: "20px"
components:
  button-primary:
    backgroundColor: "{colors.accent}"
    textColor: "{colors.accent-ink}"
    minHeight: "30px"
    padding: "4px 12px"
  button-secondary:
    backgroundColor: "{colors.surface-raised}"
    textColor: "{colors.ink}"
    border: "{colors.border-strong}"
  button-danger:
    backgroundColor: "danger-soft"
    textColor: "{colors.danger}"
  panel:
    backgroundColor: "{colors.surface}"
    border: "{colors.border}"
    header: "{colors.surface-raised}"
  kpi-tile:
    backgroundColor: "{colors.surface}"
    divider: "1px {colors.border}"
  nav-selected:
    backgroundColor: "accent-soft"
    indicator: "3px {colors.accent}"
---

# Design System: Trading Bot · 个人交易工作台

## Overview

界面参照券商交易终端与 QuantConnect 的工作区：深色底、带标题条的面板、高信息密度的表格和等宽数字。默认使用深色主题，顶栏可切换为浅色，选择保存在当前浏览器中。

顶栏按当前账户或所查看实例的执行上下文显示时钟、时区、市场及部署位置。交易状态采用后台返回的最新市场结果；没有可靠结果时显示「交易时段待确认」，不按固定美股时间猜测。模拟或实盘标识属于具体账户及实例，不作为整个工作台的固定标签。市场、币种、数量规则及执行资格的边界见 [模块化交易执行](docs/MODULAR_EXECUTION.md)。

源码依据为 [令牌](ui/src/styles/tokens.css)、[基础控件](ui/src/styles/base.css)、[应用壳](ui/src/styles/shell.css)、[功能样式](ui/src/styles/features.css)、[账户与策略样式](ui/src/styles/account-strategy.css) 与 [共享组件](ui/src/ui.jsx)。产品边界见 [PRODUCT.md](PRODUCT.md)。

## Colors

所有颜色均定义为 `:root` 上的令牌，样式文件不再直接写十六进制颜色。深色为默认值，`[data-theme='light']` 重新定义同名令牌。

- **动作色** `accent`：橙色，用于主要按钮、当前导航指示条、当前标签页下划线、输入焦点和光标。主要按钮上的文字使用 `accent-ink`。
- **信息色** `info`：文字链接与一般提示。
- **状态色** `success` / `warning` / `danger`：分别表示已核对、待处理和错误，各有对应的 `-soft` 底色与 `-border` 边线。
- **涨跌色** `gain` / `loss`：只用于盈亏、K 线与买卖方向，与状态色分开定义。默认绿涨红跌；顶栏选择「红涨」后，`[data-updown='cn']` 将两者对调，状态色不受影响。
- **中性层级** `bg` → `surface` → `surface-raised`：页面底、面板与面板标题条；`surface-inset` 用于输入框和代码区，`surface-hover` 用于行悬停。

## Typography

正文使用系统无衬线字体，字号 13px。页面标题 16px，旁边以小号大写英文标注所属区域（如 DASHBOARD、PORTFOLIO）。面板标题 13px，字段标签 11px，表头 10.5px 大写并加字距。

所有金额、数量、价格、时间与表格单元格使用 `--font-mono` 等宽字体和 `tabular-nums`，使列内数字对齐。代码编辑器使用同一等宽字体，语法高亮颜色取自 `--code-*` 令牌，随主题切换。

## Layout

桌面为固定左侧导航（196px）加工作区，顶栏在滚动时保持可见，内容最大宽度 1440px，左右留白 20px。主导航顺序为 总览、策略、运行、账户、连接、系统；当前项左侧显示橙色指示条，「总览」上的数字角标表示需要处理的事项数，浏览器标签页标题同时显示该数字。

内容以面板组织：每个 `.card` 为带边框的面板，`card-heading` 作为灰色标题条。关键数字以 KPI 磁贴呈现（总览状态、实例事实、手动调仓摘要），磁贴之间以 1px 分隔线相连。表格在自身容器内横向滚动，表头吸顶。

在 1000px 以下表单改为两列；680px 及以下导航改为顶部三列两行，表单与磁贴改为单列，页面不出现横向滚动。

## Elevation & Shapes

面板依靠边框和底色区分层级，不使用阴影。只有确认对话框带阴影和半透明遮罩，对话框的标题区与按钮区为独立色带。控件圆角 4px，标签 3px，对话框 6px。状态标签前带一个同色圆点。

## Components

### Buttons

最小高度 30px（手机 36px）。主要按钮为橙底深色字；次要按钮为 `surface-raised` 底加 `border-strong` 边线；`danger` 变体用于「关闭已撤单/拒单」等需要谨慎确认的操作。

### Tables

单元格内边距 7px 12px，行悬停加亮。订单表的状态列使用彩色标签：已成交为绿色，等待成交为蓝色，部分成交和待核对为琥珀色，已撤单和已拒绝为红色，已结束和已关闭为中性色。买卖方向使用涨跌色。

### Confirmation Dialogs

[共享 Dialog](ui/src/components/Dialog.jsx) 以原生 `showModal` 打开。提交订单前显示委托笔数、绑定账户环境，以及按当前账户币种计算的买入和卖出估算金额。「关闭已撤单/拒单」要求勾选「已在券商端核对」后才能确认。

### Status Bar Preferences

顶栏的「绿涨 / 红涨」与「深色 / 浅色」两组切换按钮写入 `localStorage`。存储不可用时使用默认值，页面照常显示。

## Do's and Don'ts

### Do:

- **Do** 新增颜色时先在 `tokens.css` 中为深色和浅色两套主题都定义令牌。
- **Do** 数字一律使用等宽字体；未知值显示「—」，不显示为零。
- **Do** 盈亏与买卖方向使用 `gain` / `loss`，系统状态使用 `success` / `warning` / `danger`。
- **Do** 保持模块身份、状态、来源、时间及不可用原因与对应内容相邻。

### Don't:

- **Don't** 在样式或组件中直接写十六进制颜色，否则主题切换会失效。
- **Don't** 用颜色单独表达状态；标签与提示必须同时给出文字。
- **Don't** 让版式改动隐藏有效运行，或改变现有的交易确认与守卫。


策略工作区在同一选中策略下展示说明、参数、代码、回测实验和运行记录。全局品牌与页眉不标注 PAPER；账户环境显示在具体账户与绑定实例旁。回测来源必须匹配选中策略，不自动切换到另一策略或实例。此调整保留终端主题与现有设计令牌。
