const MAX_AGE_MS = 30000;
const finite = value => typeof value === 'number' && Number.isFinite(value);

export const isServerDeployment = access => access?.deployment === 'server' || access?.enabled === true;

export function freshTimestamp(value, now = Date.now()) {
  if (typeof value !== 'string' || !value) return false;
  const stamp = Date.parse(value);
  return Number.isFinite(stamp) && now - stamp <= MAX_AGE_MS && stamp - now <= 5000;
}

export function serverFresh(report, receivedAt, now = Date.now()) {
  return finite(receivedAt) && receivedAt > 0 && now >= receivedAt && now - receivedAt <= MAX_AGE_MS &&
    typeof report?.checked_at === 'string' && Number.isFinite(Date.parse(report.checked_at));
}

export function nextReceipt(previousReport, report, receivedAt, now = Date.now()) {
  const stamp = typeof report?.checked_at === 'string' ? Date.parse(report.checked_at) : NaN;
  const previous = typeof previousReport?.checked_at === 'string' ? Date.parse(previousReport.checked_at) : NaN;
  if (!finite(now) || now <= 0 || !Number.isFinite(stamp)) return 0;
  if (Number.isFinite(previous)) {
    if (stamp < previous) return 0;
    if (stamp === previous) return finite(receivedAt) && receivedAt > 0 ? receivedAt : 0;
  }
  return now;
}

export function formatBytes(value) {
  if (!finite(value) || value < 0) return '—';
  if (value < 1024) return `${Math.round(value)} B`;
  const units = ['KiB', 'MiB', 'GiB', 'TiB'];
  const index = Math.min(Math.floor(Math.log(value) / Math.log(1024)) - 1, units.length - 1);
  return `${(value / 1024 ** (index + 1)).toLocaleString('en-US', { maximumFractionDigits: 1 })} ${units[index]}`;
}

export function formatUptime(value) {
  if (!finite(value) || value < 0) return '—';
  const minutes = Math.floor(value / 60), hours = Math.floor(minutes / 60), days = Math.floor(hours / 24);
  if (!minutes) return '不到 1 分钟';
  return days ? `${days} 天 ${hours % 24} 小时` : hours ? `${hours} 小时 ${minutes % 60} 分钟` : `${minutes} 分钟`;
}

export function percentMetric(value) {
  if (!finite(value) || value < 0 || value > 100) return { value: null, label: '—' };
  return { value, label: `${value.toLocaleString('en-US', { maximumFractionDigits: 1 })}%` };
}

export function capacityMetric(used, total) {
  if (!finite(used) || !finite(total) || used < 0 || total <= 0 || used > total) {
    return { value: null, label: '—', detail: '暂无可靠读数' };
  }
  return { ...percentMetric(used / total * 100), detail: `${formatBytes(used)} / ${formatBytes(total)}` };
}

export function awsLink(value) {
  if (typeof value !== 'string') return null;
  try {
    const url = new URL(value);
    const official = url.hostname === 'lightsail.aws.amazon.com' || url.hostname === 'console.aws.amazon.com' ||
      url.hostname.endsWith('.console.aws.amazon.com');
    return official && url.protocol === 'https:' && !url.username && !url.password && !url.port ? url.href : null;
  } catch { return null; }
}

export function serverOperations(report, { fresh, pending = false } = {}) {
  return ['start', 'stop'].map(id => {
    const operation = report?.operations?.find(item => item.id === id);
    const active = id === 'stop' && report?.backend?.active_job === true;
    return { id, enabled: fresh === true && !pending && operation?.enabled === true && !active,
      reason: !fresh ? '请先取得最新服务器状态。' : pending ? '正在确认操作结果。' :
        active ? '任务仍在运行，结束后可停止后台。' : operation?.reason || null };
  });
}

export function serverHealth(report, fresh) {
  const runtime = report?.runtime?.status;
  const unknown = { label: '状态未知', tone: 'muted' };
  const runtimeLabels = { stopped: '已停止', starting: '正在启动', stopping: '正在停止', failed: '异常退出', unresponsive: '未响应' };
  const backend = !fresh ? unknown : runtime === 'running' && report?.backend?.reachable === true ?
    { label: '运行中', tone: 'green' } : runtime === 'running' ? { label: '未响应', tone: 'amber' } :
      { label: runtimeLabels[runtime] || '状态未知', tone: ['failed', 'unresponsive'].includes(runtime) ? 'amber' : 'muted' };
  const automatic = report?.backend?.automation_status;
  const automation = !fresh || report?.backend?.reachable !== true ? unknown : automatic === 'paused' ?
    { label: '已暂停', tone: 'amber' } : report.backend.automation_enabled === false ? { label: '已关闭', tone: 'muted' } :
      report.backend.automation_enabled === true ? { label: automatic === 'running' ? '执行中' : automatic === 'waiting' ? '等待计划时间' : '已开启', tone: 'blue' } : unknown;
  return { portal: fresh && report?.portal?.status === 'running' ? { label: '在线', tone: 'green' } : unknown, backend, automation };
}
