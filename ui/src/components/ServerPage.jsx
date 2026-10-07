import React, { useEffect, useRef, useState } from 'react';
import { api } from '../api';
import { downloadBlob } from '../strategy_instances';
import { awsLink, capacityMetric, formatUptime, freshTimestamp, nextReceipt, percentMetric, serverFresh, serverHealth, serverOperations } from '../server';

const time = value => value && Number.isFinite(Date.parse(value)) ? new Date(value).toLocaleString('zh-CN', { hour12: false }) : '—';
const accountLabels = { connected: '最近检查通过', blocked: '最近检查未通过', paused: '最近检查待核对' };

function Metric({ title, metric, detail }) {
  return <section className="card server-metric"><span className="metric-label">{title}</span><strong className="mono">{metric.label}</strong><div className="server-meter" aria-hidden="true">{metric.value !== null && <i style={{ width: `${metric.value}%` }}/>}</div><p>{detail || metric.detail || '暂无可靠读数'}</p></section>;
}

export function ServerPage({ onNavigate, onRuntimeChange }) {
  const [diagnosticsPending, setDiagnosticsPending] = useState(false);
  const downloadingDiagnostics = useRef(false);
  const [report, setReport] = useState(null), [receivedAt, setReceivedAt] = useState(0);
  const [error, setError] = useState(''), [notice, setNotice] = useState('');
  const [loading, setLoading] = useState(true), [pending, setPending] = useState(false), [now, setNow] = useState(Date.now());
  const mounted = useRef(true), version = useRef(0), submitting = useRef(false);
  const latestSample = useRef({ report: null, receivedAt: 0 });
  const updateRuntime = useRef(onRuntimeChange);
  updateRuntime.current = onRuntimeChange;

  async function load() {
    const request = ++version.current;
    setLoading(true);
    try {
      const value = await api('server/status', undefined, { timeoutMs: 8000 });
      if (mounted.current && request === version.current) {
        const timestamp = Date.now();
        const receipt = nextReceipt(latestSample.current.report, value, latestSample.current.receivedAt, timestamp);
        latestSample.current = { report: value, receivedAt: receipt };
        setReport(value); setReceivedAt(receipt); setNow(timestamp); setError('');
      }
    } catch (failure) {
      if (mounted.current && request === version.current) { latestSample.current.receivedAt = 0; setReceivedAt(0); setError(failure.message); }
    } finally {
      if (mounted.current && request === version.current) setLoading(false);
    }
  }
  useEffect(() => {
    mounted.current = true;
    let timer;
    async function poll() {
      if (!submitting.current) await load();
      if (mounted.current) timer = setTimeout(poll, 10000);
    }
    poll();
    const clock = setInterval(() => setNow(Date.now()), 1000);
    return () => { mounted.current = false; ++version.current; clearTimeout(timer); clearInterval(clock); };
  }, []);

  const fresh = serverFresh(report, receivedAt, now) && !error;
  const metricsFresh = fresh && freshTimestamp(report?.host?.sampled_at, Date.parse(report?.checked_at));
  const host = report?.host || {}, deployment = report?.deployment || {};
  const health = serverHealth(report, fresh);
  const operations = serverOperations(report, { fresh, pending });
  const consoleUrl = awsLink(report?.links?.console), terminalUrl = awsLink(report?.links?.terminal);
  const cpu = percentMetric(metricsFresh ? host.cpu_percent : null);
  const memory = capacityMetric(metricsFresh ? host.memory_used_bytes : null, host.memory_total_bytes);
  const disk = capacityMetric(metricsFresh ? host.disk_used_bytes : null, host.disk_total_bytes);

  async function operate(id) {
    if (submitting.current || !serverOperations(report, { fresh: serverFresh(report, receivedAt), pending }).find(item => item.id === id)?.enabled) return;
    submitting.current = true; ++version.current; setPending(true); setError(''); setNotice('');
    try {
      await api(`runtime/${id}`, {});
      if (mounted.current) setNotice(id === 'start' ? '已发送启动请求，正在确认后台状态。' : '已发送停止请求，正在确认后台状态。');
    } catch (failure) {
      if (mounted.current) setNotice(failure.message);
    } finally {
      if (mounted.current) { await load(); updateRuntime.current?.(); setPending(false); }
      submitting.current = false;
    }
  }

  async function downloadDiagnostics() {
    if (downloadingDiagnostics.current) return;
    downloadingDiagnostics.current = true;
    setDiagnosticsPending(true); setError('');
    try {
      const blob = await api('server/diagnostics', undefined, { download: true });
      await downloadBlob(blob, 'trading-bot-diagnostics.json');
    } catch (failure) { if (mounted.current) setError(failure.message); }
    finally { downloadingDiagnostics.current = false; if (mounted.current) setDiagnosticsPending(false); }
  }

  return <div className="server-page">
    <div className="section-toolbar"><span className="small muted">{loading ? '正在更新服务器状态…' : fresh ? `最近更新 ${time(report.checked_at)}` : '尚未取得最新服务器状态'}</span><div className="button-row"><button className="button" disabled={loading || pending} onClick={load}>刷新状态</button><button className="button" disabled={diagnosticsPending} onClick={downloadDiagnostics}>{diagnosticsPending ? '正在准备诊断…' : '下载诊断'}</button><button className="button" onClick={() => onNavigate('logs')}>运行与日志</button></div></div>
    {error && <div className="alert error" role="alert">{error}</div>}
    {!error && report && !fresh && <div className="alert error" role="status">状态已过期，等待重新连接。旧读数不会作为当前运行状态。</div>}
    {notice && <div className="alert" role="status">{notice}</div>}
    <section className="card server-instance"><div className="card-heading"><div><h2>{deployment.instance_name || '云端服务器'}</h2><p>{deployment.provider === 'aws_lightsail' ? 'AWS Lightsail' : deployment.provider || '服务器部署'}</p></div><span className={'tag ' + (fresh ? 'blue' : 'muted')}>{fresh ? '状态已更新' : '等待确认'}</span></div><div className="server-facts"><div><span>区域</span><strong>{deployment.region || '未配置'}</strong></div><div><span>系统</span><strong>{host.os || '—'}</strong></div><div><span>服务器已运行</span><strong>{formatUptime(metricsFresh ? host.uptime_seconds : null)}</strong></div></div></section>
    <div className="server-metrics"><Metric title="CPU 使用率" metric={cpu} detail={metricsFresh && typeof host.cpu_count === 'number' && host.cpu_count > 0 ? `${host.cpu_count} 个逻辑核心` : null}/><Metric title="内存使用率" metric={memory}/><Metric title="磁盘使用率" metric={disk}/></div>
    <section className="card"><div className="card-heading"><div><h2>服务状态</h2><p>工作台负责管理；交易后台负责策略任务。</p></div></div><div className="server-services">
      <div><div className="server-service-heading"><strong>Portal 工作台</strong><span className={'tag ' + health.portal.tone}>{health.portal.label}</span></div><p>已运行 {formatUptime(fresh ? report?.portal?.uptime_seconds : null)}{report?.portal?.version ? ` · ${report.portal.version}` : ''}</p></div>
      <div><div className="server-service-heading"><strong>交易后台</strong><span className={'tag ' + health.backend.tone}>{health.backend.label}</span></div><p>{fresh && report?.backend?.active_job ? '当前有任务执行中，完成后可停止。' : '停止后台后，本页仍可用于重新启动。'}</p></div>
      <div><div className="server-service-heading"><strong>{report?.backend?.broker_name || '所选券商'}</strong><span className="tag muted">历史检查</span></div><p>{accountLabels[report?.backend?.broker_status] || '暂无检查记录'} · {time(report?.backend?.broker_checked_at)}</p><small>这里显示上次检查结果，不代表当前登录状态。</small></div>
      <div><div className="server-service-heading"><strong>自动运行</strong><span className={'tag ' + health.automation.tone}>{health.automation.label}</span></div><p>计划在「策略运行」查看；异常通知在系统中查看。</p></div>
    </div><div className="form-footer"><span className="small muted">后台停止期间不会执行定时任务；重新启动会恢复已开启的自动计划。已提交委托仍可能成交。</span><div className="button-row">{operations.map(operation => <button key={operation.id} className={'button' + (operation.id === 'start' ? ' primary' : '')} disabled={!operation.enabled} title={operation.reason || undefined} onClick={() => operate(operation.id)}>{operation.id === 'start' ? '启动后台' : '停止后台'}</button>)}</div></div></section>
    <section className="card quiet-card"><div className="card-heading"><div><h2>AWS 管理入口</h2><p>实例配置、账单或系统维护在 AWS 官方页面完成。</p></div></div><div className="button-row">{consoleUrl ? <a className="button" href={consoleUrl} target="_blank" rel="noopener noreferrer">打开 AWS 控制台 ↗</a> : <span className="small muted">未配置 AWS 控制台入口</span>}{terminalUrl ? <a className="button" href={terminalUrl} target="_blank" rel="noopener noreferrer">打开官方终端 ↗</a> : <span className="small muted">未配置终端入口，可从 AWS 控制台连接。</span>}</div></section>
  </div>;
}
