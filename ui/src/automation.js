const labels = {
  disabled: '已关闭', waiting: '等待调仓', running: '运行中',
  paused: '需要处理', unknown: '连接中', stopped: '后台已停止',
};

export function automationView(automation, { online = true, runtimeRunning = true } = {}) {
  const enabled = automation?.enabled === true;
  let status;
  if (!online) status = 'unknown';
  else if (!runtimeRunning) status = 'stopped';
  else if (!automation) status = 'unknown';
  else if (automation.status === 'paused') status = 'paused';
  else if (!enabled) status = 'disabled';
  else status = ['waiting', 'running', 'paused'].includes(automation.status) ? automation.status : 'paused';
  const tone = ['unknown', 'paused'].includes(status) ? 'amber' : ['waiting', 'running'].includes(status) ? 'blue' : 'muted';
  const message = status === 'unknown' ? '等待后台确认自动运行状态。' : status === 'stopped' ? '后台未运行，定时任务不会执行。' :
    automation?.message || (status === 'disabled' ? '服务在线；交易操作仍需手动执行。' : '按保存的计划运行交易。');
  const startup = automation?.startup?.status;
  const label = enabled && status === 'waiting' && startup === 'pending_check' ? '首次检查中' :
    enabled && status === 'waiting' && startup === 'checked' ? '等待首次建仓' : labels[status];
  return { enabled, status, tone, label, message,
    available: automation?.available === true && online && runtimeRunning,
    blockers: (automation?.blockers || []).map(item => typeof item === 'string' ? item : item.message || item.code || '尚未满足自动运行条件'),
  };
}

export function automationSchedule(config = {}) {
  return { cadence: config.cadence || 'monthly', day: config.day ?? 1, weekday: config.weekday ?? 0,
    prepare_time: config.prepare_time || '', trade_time: config.trade_time || '',
    initial_entry: config.initial_entry || 'immediate', window_minutes: config.window_minutes ?? 60,
    timezone: config.timezone || '', poll_seconds: config.poll_seconds ?? 30 };
}

export function scheduleChanged(draft, config) {
  const saved = automationSchedule(config);
  return Object.keys(saved).some(key => String(draft[key]) !== String(saved[key]));
}

export function automationControls(automation, { online = true, runtimeRunning = true, pending = false, busy = false, dirty = false, brokerVerified = false, reconcileReady = false, resolveRemainderReady = false, closeTerminalReady = false, closeUntransmittedReady = false } = {}) {
  const view = automationView(automation, { online, runtimeRunning });
  const idle = automation?.enabled === false && online && runtimeRunning && !pending && !busy && reconcileReady === true;
  return { canEnable: view.available && !view.enabled && !pending && !busy && !dirty && !view.blockers.length,
    // Pausing must remain available while a data or order task is running.
    canDisable: view.enabled && online && runtimeRunning && !pending,
    canEdit: !view.enabled && online && runtimeRunning && !pending && !busy,
    canResolveRemainder: idle && resolveRemainderReady === true,
    canCloseTerminal: idle && closeTerminalReady === true,
    canCloseUntransmitted: idle && closeUntransmittedReady === true,
  };
}

export function scheduleSummary(config = {}) {
  const cfg = automationSchedule(config);
  const days = ['周一', '周二', '周三', '周四', '周五', '周六', '周日'];
  const cadence = cfg.cadence === 'daily' ? '每个交易日' : cfg.cadence === 'weekly' ? `每${days[cfg.weekday]}（休市顺延）` :
    `每月 ${cfg.day} 日起的首个交易日`;
  return `${cadence} · ${cfg.trade_time || '时间未设置'} · ${cfg.timezone || '时区未设置'}`;
}

export function automationAccountMatches(automation, brokerId) {
  if (!automation?.enabled && automation?.status !== 'paused') return true;
  // Display names can collide. Older servers without the stable binding ID
  // cannot safely route account-specific reconciliation from a saved plan.
  return typeof automation.binding?.broker_id === 'string' && automation.binding.broker_id === brokerId;
}

export function automationOrderCheck(automation, jobs = [], selectedBrokerId) {
  const brokerId = (automation?.enabled || automation?.status === 'paused')
    ? automation.binding?.broker_id : selectedBrokerId;
  if (!brokerId) return null;
  const checks = jobs.filter(job => job.broker === brokerId &&
    ['reconcile', 'resolve-remainder', 'close-terminal', 'close-untransmitted'].includes(job.action) && job.finished_at &&
    Number.isFinite(Date.parse(job.finished_at)));
  const job = checks.reduce((latest, item) => !latest || Date.parse(item.finished_at) > Date.parse(latest.finished_at) ? item : latest, null);
  if (!job) return null;
  const recovered = ['ready', 'complete'].includes(job.status) && job.status === job.result?.recovery_state;
  const planned = recovered ? (job.result?.details?.orders || []).filter(order => order.status === 'planned').length : null;
  const message = recovered && job.status === 'ready' && planned > 0
    ? `核对通过 · ${planned} 笔未提交计划`
    : recovered && job.status === 'complete' ? '核对通过 · 无未完成委托'
    : job.message || '核对结果待确认';
  const terminal = job.action === 'reconcile' ? Number(job.result?.details?.terminal) : 0;
  const untransmitted = job.action === 'reconcile' ? Number(job.result?.details?.untransmitted) : 0;
  const titles = { 'resolve-remainder': '最近余单处理', 'close-terminal': '最近关闭撤单/拒单', 'close-untransmitted': '最近关闭未发送委托' };
  return { at: job.finished_at, title: titles[job.action] || '最近订单核对', message,
    terminal: Number.isInteger(terminal) && terminal > 0 ? terminal : 0,
    untransmitted: Number.isInteger(untransmitted) && untransmitted > 0 ? untransmitted : 0 };
}
