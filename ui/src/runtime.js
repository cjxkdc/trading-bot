export function runtimeView(runtime, online, pending, active, automation) {
  const status = online ? runtime?.status || 'starting' : 'unknown';
  const labels = { running: '后台运行中', stopped: '后台已停止', starting: '后台启动中',
    stopping: '后台停止中', failed: '后台异常退出', unresponsive: '后台未响应', unknown: '后台状态未知' };
  const canStart = ['stopped', 'failed'].includes(status) && !runtime?.alive;
  // A backend that stopped answering (or failed to start) but is still alive can
  // be asked to exit on its own; it is never killed while an order may be in flight.
  const canStop = (status === 'running' && !active) || (['unresponsive', 'failed'].includes(status) && !!runtime?.alive);
  return { status, label: labels[status] || labels.unknown,
    checked: !!runtime?.alive, disabled: !online || pending || !(canStart || canStop),
    tone: status === 'running' ? 'on' : ['unknown', 'unresponsive', 'failed'].includes(status) ? 'warning' : '',
    hint: active ? '任务执行中，结束后可关闭' : status === 'running' ? automation?.enabled ? '服务在线 · 自动运行已开启' : '服务在线 · 操作需手动执行' :
      status === 'stopped' ? '打开开关即可启动' : status === 'failed' ? (runtime?.alive ? runtime?.message || '后台进程仍在运行；关闭开关可请求它在完成在途任务后退出' : runtime?.message || '未自动重启或重试任务') :
      status === 'unresponsive' ? runtime?.message || '后台进程仍在运行但没有响应；关闭开关可请求它自行退出，不会强制终止' :
      status === 'starting' || status === 'stopping' ? '正在确认进程状态' : '请等待连接恢复' };
}
