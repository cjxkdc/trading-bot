const knownStatuses = new Set(['pending', 'running', 'passed', 'blocked', 'skipped']);

export function preflightControls(state, busy = false) {
  const noOrders = state?.preview?.status === 'complete' && state.preview.no_orders === true;
  return {
    canTest: !busy && state?.actions?.preflight?.enabled === true,
    showSubmit: !noOrders && state?.actions?.execute?.enabled === true && state?.preview?.execution_ready === true,
    noOrders,
    request: { action: 'preflight' },
  };
}

export function preflightProgress(job) {
  if (!job || job.action !== 'preflight' || !Array.isArray(job.steps)) return null;
  const steps = job.steps.map(step => ({ ...step, status: knownStatuses.has(step.status) ? step.status : 'pending',
    warnings: Array.isArray(step.warnings) ? step.warnings.filter(warning => typeof warning === 'string' && warning.trim()) : [],
  }));
  const passed = steps.filter(step => step.status === 'passed').length;
  const warnings = steps.filter(step => step.warnings.length > 0);
  const blocked = steps.find(step => step.status === 'blocked');
  const running = steps.find(step => step.status === 'running');
  const counts = `${passed} 项通过${warnings.length ? ` · ${warnings.length} 项有说明` : ''}`;
  return { steps, passed, blocked, running, warnings,
    summary: job.status === 'running' ? running?.label || '正在检查前置条件' : blocked ?
      `${counts} · ${blocked.label}待处理` : counts,
    message: blocked?.message || job.message || '',
  };
}
