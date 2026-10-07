export const normalizeCode = value => value.replace(/\r\n?/g, '\n');

export function editorBlockedReason(state, busy) {
  if (!state) return '正在读取工作台状态。';
  if (state.strategy_manager?.corrupt || state.automation?.corrupt) return '运行记录需要核对，暂时只能查看代码。';
  if (state.automation?.enabled || state.strategy_manager?.running_count || state.strategy_manager?.instances?.some(x => x.enabled)) return '先暂停自动计划和所有策略实例，再修改代码。';
  if (state.strategy_manager?.instances?.some(x => x.pending) || state.strategy_manager?.accounts?.some(x => x.pending)) return '先核对未完成的订单批次，再修改代码。';
  if (busy) return '工作台正在处理操作或连接尚未恢复。';
  return '';
}
