import { formatZoned } from './execution_context.js';

export const periods = [['1W', '近一周'], ['1M', '近一月'], ['3M', '近三月'], ['YTD', '今年'], ['ALL', '全部']];
export const finite = value => typeof value === 'number' && Number.isFinite(value);
export const money = value => finite(value) ? value.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 }) : '—';
export const signedMoney = value => finite(value) ? `${value > 0 ? '+' : ''}${money(value)}` : '—';
export const rate = value => finite(value) ? `${value > 0 ? '+' : ''}${(value * 100).toFixed(2)}%` : '—';
export const tone = value => finite(value) && value !== 0 ? value > 0 ? 'gain' : 'loss' : '';
export const day = (value, timezone) => formatZoned(value, timezone, { year:'numeric', month:'2-digit', day:'2-digit' });
export const observedTime = (value, timezone) => formatZoned(value, timezone, { year:'numeric', month:'2-digit', day:'2-digit', hour:'2-digit', minute:'2-digit', hourCycle:'h23' });

export function chartLayout(points, mode, width = 960) {
  const valid = (points || []).filter(p => finite(p[mode]) && Number.isFinite(Date.parse(p.as_of)));
  if (!valid.length) return null;
  const height = 256, left = 12, right = width - (mode === 'return_rate' ? 76 : 108), top = 16, bottom = height - 28;
  const values = valid.map(p => p[mode]);
  let low = Math.min(...values), high = Math.max(...values);
  const padding = (high - low) * .14 || Math.max(Math.abs(high) * .002, mode === 'return_rate' ? .001 : 1);
  low -= padding; high += padding;
  const start = Date.parse(valid[0].as_of), end = Date.parse(valid.at(-1).as_of);
  const y = value => bottom - (value - low) / (high - low) * (bottom - top);
  const dots = valid.map(p => ({ ...p, x: end === start ? (left + right) / 2 : left + (Date.parse(p.as_of) - start) / (end - start) * (right - left), y: y(p[mode]) }));
  return { width, height, left, right, top, bottom, dots,
    path: dots.map((p, i) => `${i ? 'L' : 'M'}${p.x},${p.y}`).join(' '),
    ticks: Array.from({ length: 4 }, (_, i) => { const value = low + (high - low) * i / 3; return { value, y: y(value) }; }) };
}

export function visiblePositions(rows, query, sort) {
  const filtered = (rows || []).filter(p => Number.isFinite(Number(p.quantity)) && Number(p.quantity) !== 0 && `${p.symbol} ${p.name || ''}`.toLowerCase().includes(query.trim().toLowerCase()));
  return filtered.sort((a, b) => {
    const av = a[sort], bv = b[sort];
    if (!finite(av)) return finite(bv) ? 1 : a.symbol.localeCompare(b.symbol);
    if (!finite(bv)) return -1;
    return bv - av || a.symbol.localeCompare(b.symbol);
  });
}
