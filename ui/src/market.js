// US equity session from the New York wall clock. Exchange holidays and early
// closes are not known here; the broker calendar remains the trading authority.
const NY = 'America/New_York';

export function newYorkParts(date = new Date()) {
  const parts = Object.fromEntries(new Intl.DateTimeFormat('en-US', {
    timeZone: NY, weekday: 'short', hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23',
  }).formatToParts(date).map(part => [part.type, part.value]));
  return { weekday: parts.weekday, hour: Number(parts.hour), minute: Number(parts.minute), second: Number(parts.second) };
}

export function marketSession(date = new Date()) {
  const { weekday, hour, minute } = newYorkParts(date);
  const minutes = hour * 60 + minute;
  if (weekday === 'Sat' || weekday === 'Sun') return { id: 'closed', label: '周末休市', tone: 'closed' };
  if (minutes >= 4 * 60 && minutes < 9 * 60 + 30) return { id: 'pre', label: '盘前', tone: 'extended' };
  if (minutes >= 9 * 60 + 30 && minutes < 16 * 60) return { id: 'regular', label: '盘中交易', tone: 'open' };
  if (minutes >= 16 * 60 && minutes < 20 * 60) return { id: 'after', label: '盘后', tone: 'extended' };
  return { id: 'closed', label: '休市', tone: 'closed' };
}

export function newYorkClock(date = new Date()) {
  const { hour, minute, second } = newYorkParts(date);
  return [hour, minute, second].map(value => String(value).padStart(2, '0')).join(':');
}
