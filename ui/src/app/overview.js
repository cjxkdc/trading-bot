import { automationView } from '../automation.js';
import { instanceStatus } from '../strategy_instances.js';

export function visiblePlan(automation) {
  return automation?.enabled === true || automation?.status === 'paused';
}

export function runningRows(state) {
  const rows = (state.strategy_manager?.instances || []).map(item => ({
    id: item.id, name: item.name, account: state.brokers?.find(b => b.id === item.broker_id)?.name || item.broker_id,
    status: instanceStatus(item), next: item.enabled ? item.next_run : null,
    enabled: item.enabled, attention: item.pending || item.status === 'paused',
    destination: { page: 'runs', view: 'instances', instance: item.id },
  }));
  // A plan can own a different account from the selected manual account.
  if (visiblePlan(state.automation)) {
    const plan = state.automation, binding = plan.binding || {};
    rows.push({ id: 'single-plan', name: `${binding.strategy_name || '单策略'} · 单策略计划`,
      account: binding.broker_name || binding.broker_id || '绑定账户待核对',
      status: automationView(plan), next: plan.enabled ? plan.next_run : null,
      enabled: plan.enabled, attention: plan.status === 'paused', destination: 'automation' });
  }
  return rows.sort((a, b) => Number(b.attention) - Number(a.attention) || Number(b.enabled) - Number(a.enabled));
}

export function attentionItems(state) {
  const items = [];
  if (state.strategy_manager?.corrupt) items.push({ id: 'manager', message: '策略运行状态无法核对，请检查实例记录。', destination: 'instances' });
  for (const item of state.strategy_manager?.instances || []) {
    if (item.pending || item.status === 'paused') items.push({ id: `instance:${item.id}`, message: `${item.name}：${item.message || (item.pending ? '有订单需要核对。' : '运行需要处理。')}`, destination: { page: 'runs', view: 'instances', instance: item.id } });
  }
  if (state.automation?.status === 'paused') items.push({ id: 'plan', message: `单策略计划：${state.automation.message || '需要核对运行状态。'}`, destination: 'automation' });
  if (state.module_issue) items.push({ id: 'module', message: state.module_issue, destination: 'modules' });
  if (state.account?.status === 'blocked') items.push({ id: 'account', message: `${state.broker?.name || '所选账户'}：${state.account.message || '连接检查未通过。'}`, destination: 'broker' });
  if (state.monitoring?.error) items.push({ id: 'monitoring', message: state.monitoring.error, destination: { page: 'system', view: 'monitoring' } });
  return items;
}

export function nextRun(rows) {
  return rows.filter(row => row.enabled && row.next && Number.isFinite(Date.parse(row.next)))
    .map(row => row.next).sort((a, b) => Date.parse(a) - Date.parse(b))[0] || null;
}
