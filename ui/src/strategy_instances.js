import { automationSchedule } from './automation.js';

export function runnableBroker(item) {
  return item?.enabled === true &&
    ['prepare', 'execute', 'reconcile', 'calendar'].every(capability => item.capabilities?.includes(capability));
}

export function instanceDraft(state, item, strategyType) {
  const selected = strategyType ? state.strategies?.find(x => x.type === strategyType && x.enabled) : null;
  if (!item && strategyType && !selected) throw new Error('所选策略已不可用，请返回策略工作区重新选择。');
  const strategy = selected && selected.type !== state.strategy.type ? { ...selected.defaults, type: selected.type } : state.strategy;
  const name = selected?.name || state.strategy_name;
  const broker = runnableBroker(state.broker) ? state.broker : state.brokers.find(runnableBroker);
  const context = broker?.execution_context || {};
  const schedule = { ...(broker?.id === state.broker?.id ? state.automation?.config : {}), ...context.schedule_defaults, timezone: context.timezone || '' };
  return item ? { name: item.name, strategy: { ...item.strategy }, broker_id: item.broker_id,
    source_id: item.source_id, budget: budgetPercent(item.budget), schedule: automationSchedule(item.schedule) } :
    { name: `${name} · 新实例`, strategy: { ...strategy },
      broker_id: broker?.id || '',
      source_id: state.sources.find(x => x.id === state.source && x.capabilities.includes('collect'))?.id || state.sources.find(x => x.enabled && x.capabilities.includes('collect'))?.id || '',
      budget: 50, schedule: automationSchedule(schedule) };
}

// Modules may leave the daily times out of their schedule defaults; a form must
// not open with empty required fields. Data refresh before the open, execution
// shortly after it, in the account's own timezone; a saved value always wins.
export function withTimeDefaults(schedule, broker) {
  const defaults = broker?.execution_context?.schedule_defaults || {};
  return { ...schedule,
    timezone: schedule.timezone || broker?.execution_context?.timezone || '',
    prepare_time: schedule.prepare_time || defaults.prepare_time || '08:00',
    trade_time: schedule.trade_time || defaults.trade_time || '09:35' };
}

// Budgets are stored as decimal strings ("0.07"). Binary floats turn 0.07 * 100
// into 7.000000000000001, which the form's step validation then rejects.
export function budgetPercent(fraction) {
  return Number((Number(fraction) * 100).toFixed(4));
}

export function budgetFraction(percent) {
  return (Number(percent) / 100).toFixed(6).replace(/0+$/, '').replace(/\.$/, '');
}

export function instancePayload(draft, fields = []) {
  const budget = Number(draft.budget);
  if (!Number.isFinite(budget) || budget <= 0 || budget > 100) throw new Error('资金比例应大于 0 且不超过 100%。');
  const strategy = { ...draft.strategy };
  for (const field of fields) if (field.type === 'json' && typeof strategy[field.name] === 'string') strategy[field.name] = JSON.parse(strategy[field.name]);
  return { ...draft, strategy, budget: budgetFraction(budget), schedule: automationSchedule(draft.schedule) };
}

export function instanceStatus(item) {
  const labels = { stopped: '已停止', checking: '检查中', running: '执行中', waiting: '按计划等待', paused: '需要处理' };
  return { label: item.pending ? '待核对订单' : labels[item.status] || '状态待核对', tone: item.pending || item.status === 'paused' ? 'amber' : item.enabled ? 'blue' : 'muted' };
}

export function latestInstancePreview(jobs, item) {
  const latest = jobs?.find(x => x.action === 'strategy-preview' && x.instance_id === item.id && x.finished_at);
  const value = latest?.result;
  return value?.status === 'complete' && value.instance_revision === item.revision &&
    value.research_only === true && value.execution_ready === false ? value : null;
}

// Cancelled/rejected orders found by the latest reconciliation of this instance's account batch.
export function terminalOrderCount(jobs, item) {
  const latest = jobs?.find(x => ['strategy-reconcile', 'strategy-resolve', 'strategy-close-terminal'].includes(x.action)
    && x.instance_id === item.id && x.finished_at);
  const count = latest?.action === 'strategy-reconcile' ? Number(latest.result?.details?.terminal) : 0;
  return Number.isInteger(count) && count > 0 ? count : 0;
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
