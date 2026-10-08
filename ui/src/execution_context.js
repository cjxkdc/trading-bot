// Display metadata comes from the selected account/module, never a platform default.
export function executionContext(broker, overrides = {}) {
  return { ...(broker?.execution_context || {}), ...Object.fromEntries(Object.entries(overrides || {}).filter(([,v]) => v != null && v !== '')) };
}
export function validTimezone(value) {
  if (typeof value !== 'string' || !value.trim()) return false;
  try { new Intl.DateTimeFormat('en', { timeZone: value }).format(); return true; } catch { return false; }
}
export const timezoneLabel = value => validTimezone(value) ? value : '时区未提供';
export const currencyLabel = value => typeof value === 'string' && value.trim() ? value : '币种未提供';
export const environmentLabel = broker => broker?.mode_label || ({ paper:'本地回放', broker_paper:'模拟账户', live:'实盘账户' })[broker?.mode] || '环境待核验';
export function formatZoned(value, timezone, options) {
  if (!value || !Number.isFinite(Date.parse(value)) || !validTimezone(timezone)) return '—';
  return new Intl.DateTimeFormat('zh-CN', { ...options, timeZone: timezone }).format(new Date(value));
}
export function brokerCanExecute(broker) {
  return broker?.enabled === true && broker.execution_enabled === true;
}
