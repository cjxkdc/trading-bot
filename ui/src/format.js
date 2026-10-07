export const actions = { 'strategy-cycle': '策略实例调仓', 'strategy-check': '检查策略实例', 'strategy-preview': '预览实例选股', 'strategy-resolve': '结束策略未提交余单', 'strategy-reconcile': '核对策略订单与归属', observe: '更新账户表现', status: '检查账户', offline: '检查本地环境', preview: '策略预览', preflight: '一键测试并准备', execute: '提交模拟订单',
  prepare: '准备模拟订单', refresh: '下载与选股', reconcile: '核对订单状态', 'resolve-remainder': '结束未提交余单', automation: '自动运行', 'data-status': '检查数据权限', 'configure-data': '保存数据配置' };
export const statuses = { running: '运行中', blocked: '未完成', paused: '待核对', connected: '检查通过',
  ready: '已就绪', previewed: '已预览', complete: '已完成', interrupted: '结果待核对' };
export const percent = value => value == null ? '—' : `${(Number(value) * 100).toFixed(1)}%`;
export const number = value => value == null ? '—' : Number(value).toLocaleString('en-US', { maximumFractionDigits: 2 });
export const time = value => value ? new Date(value).toLocaleString('zh-CN', { hour12: false }) : '—';
export const cls = (...values) => values.filter(Boolean).join(' ');
