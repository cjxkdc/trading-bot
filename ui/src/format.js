export const actions = { 'strategy-cycle': '策略实例调仓', 'strategy-check': '检查策略实例', 'strategy-preview': '预览实例选股', 'strategy-resolve': '结束策略未提交余单', 'strategy-reconcile': '核对策略订单与归属', 'strategy-close-terminal': '关闭策略撤单/拒单', observe: '更新账户表现', status: '检查账户', offline: '检查本地环境', preview: '策略预览', preflight: '一键测试并准备', execute: '提交订单',
  prepare: '准备订单', refresh: '下载与选股', reconcile: '核对订单状态', 'resolve-remainder': '结束未提交余单', 'close-terminal': '关闭已撤单/拒单', 'close-untransmitted': '确认未发送并关闭', automation: '自动运行', 'data-status': '检查数据权限', 'configure-data': '保存数据配置', backfill: '历史回填' };
export const statuses = { running: '运行中', blocked: '未完成', paused: '待核对', connected: '检查通过',
  ready: '已就绪', previewed: '已预览', complete: '已完成', interrupted: '结果待核对' };
export const percent = value => value == null ? '—' : `${(Number(value) * 100).toFixed(1)}%`;
export const number = value => value == null ? '—' : Number(value).toLocaleString('en-US', { maximumFractionDigits: 2 });
export const time = value => value ? new Date(value).toLocaleString('zh-CN', { hour12: false }) : '—';
export const cls = (...values) => values.filter(Boolean).join(' ');

// "约 2 小时后" / "5 分钟前": a quick reading beside the exact local timestamp.
export function relativeTime(value, now = Date.now()) {
  const stamp = value ? Date.parse(value) : NaN;
  if (!Number.isFinite(stamp)) return '';
  const seconds = Math.round((stamp - now) / 1000), size = Math.abs(seconds);
  if (size < 45) return seconds >= 0 ? '即将开始' : '刚刚';
  const [amount, unit] = size < 3600 ? [Math.round(size / 60), '分钟'] : size < 86400 ? [Math.round(size / 3600), '小时'] : [Math.round(size / 86400), '天'];
  return seconds > 0 ? `约 ${amount} ${unit}后` : `${amount} ${unit}前`;
}


// Quantities and prices are decimal strings in the execution protocol. Never
// round them to money precision or pass exact strings through binary Number.
export function decimal(value) {
  if (!['string','number','bigint'].includes(typeof value)) return '—';
  if (typeof value === 'number' && (!Number.isFinite(value) || Math.abs(value) > Number.MAX_SAFE_INTEGER)) return '—';
  const raw = String(value).trim();
  if (!raw || raw.length > 2048) return '—';
  const match = /^([+-]?)(\d*)(?:\.(\d*))?(?:[eE]([+-]?\d+))?$/.exec(raw);
  if (!match || !(match[2] || match[3])) return '—';
  const exponent = Number(match[4] || 0);
  if (!Number.isSafeInteger(exponent) || Math.abs(exponent) > 1000) return '—';
  const digits = match[2] + (match[3] || ''), point = match[2].length + exponent;
  let whole, fraction;
  if (point <= 0) { whole = '0'; fraction = '0'.repeat(-point) + digits; }
  else if (point >= digits.length) { whole = digits + '0'.repeat(point - digits.length); fraction = ''; }
  else { whole = digits.slice(0, point); fraction = digits.slice(point); }
  whole = whole.replace(/^0+(?=\d)/, '').replace(/\B(?=(\d{3})+(?!\d))/g, ',');
  const sign = match[1] === '-' && /[1-9]/.test(digits) ? '-' : '';
  return `${sign}${whole}${fraction ? '.' + fraction : ''}`;
}
