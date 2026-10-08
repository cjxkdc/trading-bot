import { validTimezone } from './execution_context.js';

export const candlePeriods = [
  { id: '1m', label: '1分钟' }, { id: '5m', label: '5分钟' },
  { id: '1h', label: '小时K' }, { id: '1d', label: '日K' }, { id: '1w', label: '周K' },
];

export function candleLayout(bars, width = 960, height = 260) {
  if (!Array.isArray(bars) || !bars.length) return null;
  const times = bars.map(b => [Date.parse(b.start), Date.parse(b.end)]);
  if (bars.some((b,i) => ![b.open,b.high,b.low,b.close,...times[i]].every(Number.isFinite)
      || b.low <= 0 || b.low > Math.min(b.open,b.close) || b.high < Math.max(b.open,b.close)
      || times[i][1] <= times[i][0] || (i && times[i][0] < times[i-1][1])
      || !Number.isInteger(b.sample_count) || b.sample_count < 1)) return null;
  width = Math.max(320, Number.isFinite(width) ? width : 960);
  const left = 12, right = width - 112, top = 16, bottom = height - 32;
  const low = Math.min(...bars.map(b => b.low)), high = Math.max(...bars.map(b => b.high));
  const padding = Math.max((high-low)*.12, Math.abs(high)*.00001, .01);
  const min = Math.max(0, low-padding), max = high+padding;
  const first = times[0][0], last = times.at(-1)[1];
  const x = at => left + (at-first)/(last-first)*(right-left);
  const y = value => bottom - (value-min)/(max-min)*(bottom-top);
  const points = bars.map((bar,i) => ({ ...bar, index:i, x:x((times[i][0]+times[i][1])/2),
    width:Math.max(1.25,Math.min(14,(x(times[i][1])-x(times[i][0]))*.55)),
    yOpen:y(bar.open), yHigh:y(bar.high), yLow:y(bar.low), yClose:y(bar.close),
    tone:bar.close > bar.open ? 'up' : bar.close < bar.open ? 'down' : 'flat',
  }));
  return { width, height, left, right, top, bottom, points,
    ticks:Array.from({ length:5 },(_,i) => ({ y:top+(bottom-top)*i/4, value:max-(max-min)*i/4 })),
  };
}

export function nearestCandle(points, x) {
  if (!points?.length || !Number.isFinite(x)) return null;
  return points.reduce((closest,p) => Math.abs(p.x-x) < Math.abs(closest.x-x) ? p : closest, points[0]).index;
}

export function moveCandle(index, key, count) {
  if (!count) return null;
  const current = Number.isInteger(index) ? index : count-1;
  if (key === 'Home') return 0;
  if (key === 'End') return count-1;
  if (key === 'ArrowLeft') return Math.max(0,current-1);
  if (key === 'ArrowRight') return Math.min(count-1,current+1);
  return current;
}

const dateFormat = (timezone, options) => new Intl.DateTimeFormat('zh-CN', { timeZone:timezone, ...options });
export function candleLabel(bar, interval, timezone) {
  if (!bar || !Number.isFinite(Date.parse(bar.start)) || !validTimezone(timezone)) return '—';
  if (interval === '1w') {
    const f = dateFormat(timezone, { year:'numeric',month:'2-digit',day:'2-digit' });
    return `${f.format(new Date(bar.start))} — ${f.format(new Date(Date.parse(bar.end)-1))}`;
  }
  return dateFormat(timezone, { year:'numeric',month:'2-digit',day:'2-digit',
    ...(interval === '1d' ? {} : {hour:'2-digit',minute:'2-digit',hour12:false,timeZoneName:'short'}),
  }).format(new Date(bar.start));
}

export function candleAxisLabel(bar, interval, timezone) {
  if (!bar || !Number.isFinite(Date.parse(bar.start)) || !validTimezone(timezone)) return '—';
  return dateFormat(timezone, ['1d','1w'].includes(interval) ? {month:'2-digit',day:'2-digit'}
    : interval === '1h' ? {month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false}
    : {hour:'2-digit',minute:'2-digit',hour12:false}).format(new Date(bar.start));
}
