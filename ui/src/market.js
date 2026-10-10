import { validTimezone } from './execution_context.js';

// Only the server's selected-market calendar can describe a trading session.
// The display never derives exchange holidays, breaks or opening hours itself.
export function marketSession(session, now = new Date()) {
  const at = Date.parse(session?.observed_at || session?.checked_at);
  const fresh = Number.isFinite(at) && Math.abs(now.getTime() - at) <= 120000;
  const labels = { open:'交易中', regular:'交易中', pre:'盘前', after:'盘后', break:'休市间隔', closed:'休市' };
  const id = session?.status || session?.id;
  if (!fresh || !labels[id]) return { id:'unknown', label:'交易时段待确认', tone:'closed' };
  return { id, label:session.label || labels[id], tone:['open','regular'].includes(id) ? 'open' : ['pre','after'].includes(id) ? 'extended' : 'closed' };
}
export function marketClock(date = new Date(), timezone) {
  return new Intl.DateTimeFormat('zh-CN', { ...(validTimezone(timezone) ? { timeZone:timezone } : {}), hour:'2-digit', minute:'2-digit', second:'2-digit', hourCycle:'h23' }).format(date);
}
