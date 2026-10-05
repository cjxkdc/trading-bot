import { automationSchedule } from './automation.js';

export function runnableBroker(item) {
  return item.enabled && item.mode === 'broker_paper' &&
    ['prepare', 'execute', 'reconcile', 'calendar'].every(capability => item.capabilities?.includes(capability));
}

export function instanceDraft(state, item) {
  return item ? { name: item.name, strategy: { ...item.strategy }, broker_id: item.broker_id,
    source_id: item.source_id, budget: Number(item.budget) * 100, schedule: automationSchedule(item.schedule) } :
    { name: `${state.strategy_name} · 新实例`, strategy: { ...state.strategy },
      broker_id: runnableBroker(state.broker) ? state.broker.id : state.brokers.find(runnableBroker)?.id || '',
      source_id: state.sources.find(x => x.id === state.source && x.capabilities.includes('collect'))?.id || state.sources.find(x => x.enabled && x.capabilities.includes('collect'))?.id || '',
      budget: 50, schedule: automationSchedule(state.automation?.config) };
}

export function instancePayload(draft, fields = []) {
  const budget = Number(draft.budget);
  if (!Number.isFinite(budget) || budget <= 0 || budget > 100) throw new Error('资金比例应大于 0 且不超过 100%。');
  const strategy = { ...draft.strategy };
  for (const field of fields) if (field.type === 'json' && typeof strategy[field.name] === 'string') strategy[field.name] = JSON.parse(strategy[field.name]);
  return { ...draft, strategy, budget: String(budget / 100), schedule: automationSchedule(draft.schedule) };
}

export function instanceStatus(item) {
  const labels = { stopped: '已停止', checking: '检查中', running: '执行中', waiting: '按计划等待', paused: '需要处理' };
  return { label: labels[item.status] || '状态待核对', tone: item.pending || item.status === 'paused' ? 'amber' : item.enabled ? 'blue' : 'muted' };
}

export function budgetSummary(items, brokerId) {
  return items.filter(x => x.broker_id === brokerId && (x.enabled || x.positions?.length))
    .reduce((sum, x) => sum + Number(x.budget), 0);
}

export async function downloadBlob(blob, filename) {
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url; link.download = filename; link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
