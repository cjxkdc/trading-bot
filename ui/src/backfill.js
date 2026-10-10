// Historical backfill helpers: pure text and date logic, shared by the card and its tests.
const CADENCE_LABELS = { monthly: '每月', weekly: '每周' };
export const QUEUE_LABELS = { running: ['进行中', 'blue'], paused: ['已暂停', 'amber'], finished: ['已完成', 'green'], cancelled: ['已取消', 'muted'] };
export const DATE_LABELS = { pending: ['等待', 'muted'], running: ['采集中', 'blue'], done: ['已完成', 'green'], failed: ['失败', 'red'], skipped: ['已有快照', 'muted'], cancelled: ['已取消', 'muted'] };

const iso = day => `${day.getFullYear()}-${String(day.getMonth() + 1).padStart(2, '0')}-${String(day.getDate()).padStart(2, '0')}`;

// Twelve months back from the first of the current month, ending today.
export function defaultBackfillRange(today = new Date()) {
  const start = new Date(today.getFullYear(), today.getMonth() - 12, 1);
  return { start: iso(start), end: iso(today), cadence: 'monthly' };
}

export function durationLabel(minutes) {
  const total = Math.max(0, Math.round(Number(minutes) || 0));
  if (total < 60) return `${total} 分钟`;
  const hours = Math.floor(total / 60), rest = total % 60;
  return rest ? `${hours} 小时 ${rest} 分钟` : `${hours} 小时`;
}

export function cadenceLabel(cadence) { return CADENCE_LABELS[cadence] || cadence; }

// One sentence for a finished or cancelled queue; null while nothing has run.
export function backfillSummary(queue) {
  if (!queue || !QUEUE_LABELS[queue.status]) return null;
  const counts = queue.counts || {};
  const parts = [`完成 ${counts.done || 0} 个日期`];
  if (counts.failed) parts.push(`失败 ${counts.failed}`);
  if (counts.skipped) parts.push(`跳过 ${counts.skipped}`);
  if (counts.cancelled) parts.push(`取消 ${counts.cancelled}`);
  return `${queue.start} → ${queue.end}（${cadenceLabel(queue.cadence)}）：${QUEUE_LABELS[queue.status][0]}，${parts.join('，')}。`;
}

export function backfillPercent(queue) {
  if (!queue || !queue.total) return 0;
  return Math.round((queue.finished_count || 0) / queue.total * 100);
}
