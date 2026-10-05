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
    automation?.message || (status === 'disabled' ? '服务在线；交易操作仍需手动执行。' : '按保存的计划运行模拟交易。');
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
    prepare_time: config.prepare_time || '06:00', trade_time: config.trade_time || '09:35',
    initial_entry: config.initial_entry || 'immediate', window_minutes: config.window_minutes ?? 60,
    timezone: config.timezone || 'America/New_York', poll_seconds: config.poll_seconds ?? 30 };
}

export function scheduleChanged(draft, config) {
  const saved = automationSchedule(config);
  return Object.keys(saved).some(key => String(draft[key]) !== String(saved[key]));
}

export function automationControls(automation, { online = true, runtimeRunning = true, pending = false, busy = false, dirty = false, brokerVerified = false, reconcileReady = false, resolveRemainderReady = false } = {}) {
  const view = automationView(automation, { online, runtimeRunning });
  return { canEnable: view.available && !view.enabled && !pending && !busy && !dirty && !view.blockers.length,
    // Pausing must remain available while a data or order task is running.
    canDisable: view.enabled && online && runtimeRunning && !pending,
    canEdit: !view.enabled && online && runtimeRunning && !pending && !busy,
    canResolveRemainder: automation?.enabled === false && online && runtimeRunning && !pending && !busy &&
      reconcileReady === true && resolveRemainderReady === true,
  };
}

export function scheduleSummary(config = {}) {
  const cfg = automationSchedule(config);
  const days = ['周一', '周二', '周三', '周四', '周五'];
  const cadence = cfg.cadence === 'daily' ? '每个交易日' : cfg.cadence === 'weekly' ? `每${days[cfg.weekday]}（休市顺延）` :
    `每月 ${cfg.day} 日起的首个交易日`;
  return `${cadence} · ${cfg.trade_time} 美东时间`;
}
