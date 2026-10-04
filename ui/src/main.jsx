import React, { useEffect, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { api, connect, downloadCode } from './api';
import { runtimeView } from './runtime';
import { automationView } from './automation';
import { orderSide, orderStatus } from './orders';
import { isServerDeployment } from './server';
import { preflightControls } from './preflight';
import { PortalAccess } from './login';
import './style.css';
import { ModuleIssues } from './components/ModuleIssues';
import { StrategyReadme } from './components/StrategyReadme';
import { ModuleFields } from './components/ModuleFields';
import { PreflightProgress } from './components/PreflightProgress';
import { AutomationCard } from './components/AutomationCard';
import { ServerPage } from './components/ServerPage';

const basePages = [
  ['home', '总览', 'grid'], ['strategies', '策略库', 'layers'], ['data', '数据中心', 'database'],
  ['broker', '券商与账户', 'link'], ['positions', '持仓与订单', 'briefcase'],
  ['backtests', '回测实验室', 'chart'], ['logs', '运行与日志', 'list'],
];
const actions = { status: '检查账户', offline: '检查本地环境', preview: '策略预览', preflight: '一键测试并准备', execute: '提交模拟订单',
  prepare: '准备模拟订单', refresh: '下载与选股', reconcile: '核对订单状态', 'resolve-remainder': '结束未提交余单', automation: '自动运行', 'data-status': '检查数据权限', 'configure-data': '保存数据配置' };
const statuses = { running: '运行中', blocked: '未完成', paused: '待核对', connected: '检查通过',
  ready: '已就绪', previewed: '已预览', complete: '已完成', interrupted: '结果待核对' };
const percent = value => value == null ? '—' : `${(Number(value) * 100).toFixed(1)}%`;
const number = value => value == null ? '—' : Number(value).toLocaleString('en-US', { maximumFractionDigits: 2 });
const time = value => value ? new Date(value).toLocaleString('zh-CN', { hour12: false }) : '—';
const cls = (...values) => values.filter(Boolean).join(' ');

function Icon({ name, size = 18 }) {
  const paths = {
    grid: <><rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/></>,
    layers: <><path d="m12 3 9 5-9 5-9-5 9-5Zm-9 9 9 5 9-5M3 16l9 5 9-5"/></>,
    database: <><ellipse cx="12" cy="5" rx="8" ry="3"/><path d="M4 5v14c0 4 16 4 16 0V5M4 12c0 4 16 4 16 0"/></>,
    link: <><path d="m9 15 6-6M8 16l-1 1a4.2 4.2 0 0 1-6-6l4-4a4.2 4.2 0 0 1 6 0m2 10a4.2 4.2 0 0 0 6 0l4-4a4.2 4.2 0 0 0-6-6l-1 1" transform="translate(1 0) scale(.92)"/></>,
    briefcase: <><rect x="3" y="7" width="18" height="14" rx="2"/><path d="M8 7V4h8v3M3 12c6 4 12 4 18 0M10 13v3h4v-3"/></>,
    chart: <><path d="M4 3v17h17M7 14l4-4 4 2 5-7"/></>,
    list: <><path d="M9 5h12M9 12h12M9 19h12M3 5h1M3 12h1M3 19h1"/></>,
    arrow: <><path d="M5 12h14m-5-5 5 5-5 5"/></>,
    refresh: <><path d="M20 8a8 8 0 0 0-14-3L3 8m0-5v5h5M4 16a8 8 0 0 0 14 3l3-3m0 5v-5h-5"/></>,
    check: <path d="m5 12 4 4L19 6"/>,
    clock: <><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></>,
    upload: <><path d="M12 16V3m-5 5 5-5 5 5M4 16v5h16v-5"/></>,
    power: <><path d="M12 2v10M6 5a9 9 0 1 0 12 0"/></>,
    server: <><rect x="3" y="3" width="18" height="7" rx="2"/><rect x="3" y="14" width="18" height="7" rx="2"/><path d="M7 6.5h.01M7 17.5h.01M13 6.5h4M13 17.5h4"/></>,
    close: <path d="m6 6 12 12M6 18 18 6"/>,
  };
  return <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{paths[name] || paths.grid}</svg>;
}
function Button({ children, primary, icon, className, ...props }) {
  return <button className={cls('button', primary && 'primary', className)} {...props}>{icon && <Icon name={icon}/>}<span>{children}</span></button>;
}
function Empty({ title, children, icon = 'layers' }) {
  return <div className="empty"><span className="empty-icon"><Icon name={icon} size={24}/></span><strong>{title}</strong><p>{children}</p></div>;
}
function Tag({ children, tone = 'muted' }) { return <span className={'tag ' + tone}>{children}</span>; }
function RuntimeControl({ view, onToggle }) {
  return <div className="runtime-control"><div><strong className={view.tone}><i/>{view.label}</strong><small>{view.hint}</small></div><button type="button" role="switch" aria-label="交易后台开关" aria-checked={view.checked} disabled={view.disabled} title={view.hint} className={cls('runtime-switch', view.checked && 'checked', view.tone === 'warning' && 'warning')} onClick={onToggle}><span/></button></div>;
}
function TargetTable({ preview }) {
  const rows = preview?.targets || [];
  if (!rows.length) return <Empty title={preview ? '本次没有选中股票' : '尚未生成目标持仓'}>{preview ? '检查筛选条件与数据覆盖范围。' : '更新数据或使用已有快照，运行一次策略预览。'}</Empty>;
  return <div className="table-wrap"><table><thead><tr><th>股票</th><th>行业</th><th className="right">策略得分</th><th className="right">目标权重</th></tr></thead><tbody>{rows.map(row => <tr key={row.asset_id}><td><strong>{row.ticker || row.asset_id}</strong></td><td className="muted">{row.industry || '—'}</td><td className="right mono">{Number(row.score ?? 0).toFixed(3)}</td><td className="right mono"><span className="weight-cell"><i style={{ width: `${Math.min(100, row.weight * 200)}%` }}/><span>{percent(row.weight)}</span></span></td></tr>)}</tbody></table></div>;
}

function App({ access, onLogout }) {
  const serverDeployment = isServerDeployment(access);
  const pages = serverDeployment ? [...basePages, ['server', '服务器', 'server']] : basePages;
  const [state, setState] = useState(null), [page, setPage] = useState('home');
  const [error, setError] = useState(''), [notice, setNotice] = useState(''), [online, setOnline] = useState(false);
  const [pending, setPending] = useState(false), [stopped, setStopped] = useState(false), [dialog, setDialog] = useState(null);
  const [runtime, setRuntime] = useState(null), [heartbeat, setHeartbeat] = useState(null);
  const selectedFile = useRef(null), mounted = useRef(true), pendingRef = useRef(false), lastJob = useRef(null);
  const refreshVersion = useRef(0);
  async function refresh() {
    const version = ++refreshVersion.current;
    const current = await api('runtime', undefined, { timeoutMs: 5000 });
    if (!mounted.current || version !== refreshVersion.current) return;
    setRuntime(current); setHeartbeat(current.checked_at); setOnline(true);
    if (current.status !== 'running') { setState(null); return; }
    try {
      const data = await api('state');
      if (mounted.current && version === refreshVersion.current) setState(data);
    } catch (e) {
      if (mounted.current && version === refreshVersion.current) { setState(null); setError(e.message); }
    }
  }
  useEffect(() => {
    mounted.current = true;
    let timer;
    async function poll(first = false) {
      try { if (first) await connect(); await refresh(); }
      catch (e) { if (mounted.current) { setOnline(false); setError(e.message); } }
      if (mounted.current) timer = setTimeout(() => poll(), 2500);
    }
    poll(true);
    return () => { mounted.current = false; clearTimeout(timer); };
  }, []);
  useEffect(() => {
    const job = state?.jobs.find(item => item.id === lastJob.current);
    if (job && job.status !== 'running') {
      lastJob.current = null;
      if (job.action === 'preflight') return; // The per-step result preserves successful checks and the exact blocker.
      if (['blocked', 'paused', 'interrupted'].includes(job.status)) setError(job.message);
      else setNotice(job.message);
    }
  }, [state]);
  async function act(fn, success = '') {
    if (pendingRef.current || stopped) return;
    pendingRef.current = true; setPending(true); setError(''); setNotice('');
    try { const result = await fn(); if (result?.job_id) lastJob.current = result.job_id; setNotice(success || result?.message || ''); await refresh(); return result; }
    catch (e) { setError(e.message); }
    finally { pendingRef.current = false; setPending(false); }
  }
  const busy = pending || !!state?.active || !online || stopped || runtime?.status !== 'running';
  const runtimeStatus = runtimeView(runtime, online && !stopped, pending, state?.active, state?.automation);
  const automationStatus = automationView(state?.automation, { online: online && !stopped, runtimeRunning: runtime?.status === 'running' });
  async function toggleRuntime() {
    if (runtimeStatus.disabled) return;
    await act(async () => {
      const result = await api(runtimeStatus.status === 'running' ? 'runtime/stop' : 'runtime/start', {});
      return { ...result, message: '' }; // The live status is authoritative during transitions.
    });
  }
  const run = action => act(() => api('jobs', { action }));
  const chooseSource = source => act(() => api('source', { source }));
  const chooseBroker = broker => act(() => api('broker', { broker }));
  async function authorizeBroker() {
    const result = await act(() => api('broker-auth/start', {}, { timeoutMs: 35000 }));
    if (result?.authorization_url) window.location.assign(result.authorization_url);
  }
  const bindBroker = account => act(() => api('broker-auth/bind', { account }));
  const disconnectBroker = () => act(() => api('broker/disconnect', {}));
  const selectedData = state?.sources.find(item => item.id === state.source);
  const dataCan = capability => selectedData?.capabilities?.includes(capability);
  async function upload(file) {
    if (!file) return;
    await act(() => api('snapshot', file, { raw: true }), '快照已导入，请点击预览。');
  }
  const navigate = id => { setPage(id); setNotice(''); setError(''); window.scrollTo({ top: 0, behavior: 'auto' }); };
  const account = state?.account, connected = account?.status === 'connected';
  const accountDetails = connected ? account.details : null;
  const preview = state?.preview, snapshot = state?.snapshot;
  const allowed = action => state?.actions?.[action]?.enabled === true;
  const preflight = preflightControls(state, busy);

  async function confirm() {
    const kind = dialog; setDialog(null);
    if (kind === 'execute') await act(() => api('jobs', { action: 'execute', confirmation: state.broker.id }));
    if (kind === 'stop') {
      try { await api('shutdown', {}); mounted.current = false; setStopped(true); setOnline(false); setError(''); }
      catch (e) { setError(e.message); }
    }
  }
  return <div className="app">
    <aside className="sidebar">
      <div className="brand"><span className="brand-mark"><Icon name="chart" size={23}/></span><div>Trading Bot<small>个人交易工作台</small></div></div>
      <div className="nav-caption">工作空间</div>
      <nav aria-label="主导航">{pages.map(([id, label, icon]) => <button key={id} aria-current={page === id ? 'page' : undefined} className={cls('nav-item', page === id && 'active')} onClick={() => navigate(id)}><Icon name={icon}/><span>{label}</span>{page === id && <span className="nav-dot"/>}</button>)}</nav>
      <div className="sidebar-bottom"><span className="local-status"><i className={online && !stopped ? 'on' : ''}/>{stopped ? '工作台已退出' : online ? '面板已连接' : '面板连接中断'}</span><small>{heartbeat ? `最近确认 ${time(heartbeat)}` : '正在检查进程状态'}</small><small>关闭网页不会停止已开启的后台</small></div>
    </aside>
    <div className="workspace">
      <header className="topbar"><div className="breadcrumb">工作台 <span>/</span> <strong>{pages.find(p => p[0] === page)?.[1]}</strong>{access.enabled && <Tag tone="blue">服务器</Tag>}</div><div className="topbar-right">{access.enabled && <Button onClick={onLogout}>退出登录</Button>}<span className="mode"><i/>{state?.broker?.name || '模块工作台'}</span><button className="automation-topbar" onClick={() => navigate('logs')} title="查看自动运行计划"><Tag tone={automationStatus.tone}>自动运行 · {automationStatus.label}</Tag></button>{page !== 'server' && <RuntimeControl view={runtimeStatus} onToggle={toggleRuntime}/>}</div></header>
      <main>
        <div className="page-heading"><div><p className="eyebrow">{page === 'home' ? 'WORKSPACE' : 'TRADING BOT'}</p><h1>{page === 'home' ? '交易工作台' : pages.find(p => p[0] === page)?.[1]}</h1><p className="subtitle">{({ home: '查看组合与账户，管理建仓和调仓。', strategies: '更换策略、调整参数，沿用同一套运行框架。', data: '数据源与券商独立。每次选股使用一份固定快照。', broker: '选择账户模块，沿用各自的配置和独立账本。', positions: '查看最近一次账户检查，以及本程序的订单记录。', backtests: '导出代码，在 QuantConnect 运行，再将结果带回来。', logs: '查看每次操作的状态与结果。', server: '查看云端资源与服务状态，管理交易后台。' })[page]}</p></div><span className="heading-icon"><Icon name={pages.find(p => p[0] === page)?.[2]} size={26}/></span></div>
        {page !== 'server' && !stopped && error && <div className="alert error" role="alert"><span>{error}</span><button onClick={() => setError('')} aria-label="关闭错误"><Icon name="close" size={16}/></button></div>}
        {page !== 'server' && !stopped && notice && <div className="alert" role="status"><span>{notice}</span><button onClick={() => setNotice('')} aria-label="关闭提示"><Icon name="close" size={16}/></button></div>}
        {page === 'server' && serverDeployment ? <ServerPage onNavigate={navigate} onRuntimeChange={() => refresh().catch(() => {})}/> : stopped ? <section className="card"><Empty title="工作台已退出" icon="power">再次双击 Trading Bot.exe 即可打开。</Empty></section> : runtime?.status !== 'running' ? <section className="card runtime-empty"><Empty title={runtimeStatus.label} icon="power">{runtimeStatus.status === 'stopped' ? '面板保持打开。使用右上角开关重新启动后台，再手动选择操作。' : runtimeStatus.status === 'failed' ? '后台没有自动重启或重试任务。重新开启后，请核对运行记录。' : runtimeStatus.hint}</Empty>{page === 'logs' && !access?.enabled && <Button disabled={!online || pending || ['starting', 'stopping'].includes(runtime?.status)} onClick={() => setDialog('stop')}>退出整个工作台</Button>}</section> : !state ? <section className="card"><Empty title={error ? '暂时无法读取后台数据' : '正在读取后台数据'} icon="link">{error ? '请核对错误提示，页面会继续检查后台状态。' : '正在读取本地配置…'}</Empty></section> : <>
          {state.module_issue && <div className="alert error" role="alert">{state.module_issue}</div>}
          {state.active && <div className="running-strip" role="status"><span className="spinner"/><div><strong>{actions[state.active.action]}</strong><span>{state.active.message}</span></div>{state.active.cancel_available && <Button onClick={() => act(() => api('cancel-download', {}))} disabled={pending}>停止下载</Button>}</div>}
          {page === 'home' && <>
            <AutomationCard compact state={state} online={online} runtimeRunning={runtime?.status === 'running'} pending={pending} busy={busy} act={act}/>
            <div className="summary-row"><div><span className="metric-label">当前策略</span><strong>{state.strategy_name}</strong><button className="text-link" onClick={() => navigate('strategies')}>查看规则 <Icon name="arrow" size={14}/></button></div><div><span className="metric-label">数据日期 · {state.sources.find(s => s.id === state.source)?.name}</span><strong>{snapshot?.price_as_of || '暂无快照'}</strong><small>{snapshot ? `${snapshot.usable_count} 只有效 / ${snapshot.candidate_count} 只候选` : '下载或导入后开始预览'}</small></div><div><span className="metric-label">账户现金 · {state.broker.mode_label}</span><strong className="mono">{connected ? `${accountDetails.currency || ''} ${number(accountDetails.cash)}` : '—'}</strong><small>{connected ? `检查于 ${time(account.checked_at)}` : '尚未检查账户连接'}</small></div></div>
            <div className="home-grid"><section className="card main-card"><div className="card-heading"><div><h2>本次选股</h2><p>{preview ? `${state.strategy_name} · ${time(preview.created_at)}` : '用当前策略生成目标组合'}</p></div><Tag tone={preview ? 'blue' : 'muted'}>{preview ? preview.execution_ready ? '预览已就绪' : '研究预览' : '等待预览'}</Tag></div>
              <div className="workflow preflight-workflow"><label>数据源<select aria-label="首页数据源" value={state.source} disabled={busy} onChange={e => chooseSource(e.target.value)}>{state.sources.map(s => <option key={s.id} value={s.id} disabled={!s.enabled} title={s.reason}>{s.name}{!s.enabled && ' · 不可用'}</option>)}</select></label><div className="preflight-start"><Button primary disabled={!preflight.canTest} title={state.actions?.preflight?.message} icon="check" onClick={() => act(() => api('jobs', preflight.request))}>{state.active?.action === 'preflight' ? '正在测试…' : '一键测试并准备'}</Button></div></div>
              <details className="preflight-advanced"><summary>分步操作</summary><div className="button-row">{dataCan('collect') && <Button disabled={busy || !allowed('refresh')} title={state.actions?.refresh?.message} icon="refresh" onClick={() => run('refresh')}>更新并预览</Button>}<Button disabled={busy || !allowed('preview')} title={state.actions?.preview?.message} onClick={() => run('preview')}>预览已有数据</Button>{state.broker.capabilities.includes('prepare') && <Button disabled={busy || !allowed('prepare')} title={state.actions?.prepare?.message} onClick={() => run('prepare')}>仅准备订单</Button>}</div></details>
              <PreflightProgress job={state.preflight_job} onNavigate={navigate}/>
              {state.compatibility_issue && <p className="inline-warning">{state.compatibility_issue}</p>}<TargetTable preview={preview}/>
              {preview?.targets?.length > 0 && <div className="allocation"><span>目标股票仓位 <strong>{percent(preview.targets.reduce((a, b) => a + b.weight, 0))}</strong></span><span>其余保留为现金</span></div>}
              {preview?.orders?.length > 0 && <div className="table-wrap"><table><thead><tr><th>股票</th><th>方向</th><th className="right">股数</th><th className="right">限价（USD）</th><th className="right">金额（USD）</th></tr></thead><tbody>{preview.orders.map((o,i) => <tr key={i}><td>{o.ticker}</td><td>{orderSide(o.side)}</td><td className="right mono">{number(o.quantity)}</td><td className="right mono">{number(o.limit_price)}</td><td className="right mono">{number(o.notional)}</td></tr>)}</tbody></table><p className="small muted">报价预览仅短期有效；执行前再次检查账户和行情。超过单笔上限的调仓已拆单。</p></div>}<div className="execution-footer"><div><strong>{state.automation?.enabled ? '自动运行已开启' : preflight.noOrders ? '本轮无需提交订单' : preflight.showSubmit ? '模拟订单已准备，等待你的确认' : '准备下一轮'}</strong><p>{state.automation?.enabled ? state.automation.message : preflight.noOrders ? preview.message : preflight.showSubmit ? '核对股票、数量和当前模拟账户后，再提交给券商。' : state.preflight_job ? state.actions?.execute?.message : '点击「一键测试并准备」，前置检查会自动依次完成。'}</p></div>{preflight.showSubmit && <Button primary disabled={busy} onClick={() => setDialog('execute')}>提交模拟订单</Button>}</div>
              {preview?.issues?.length > 0 && <details className="details"><summary>查看数据说明（{preview.issues.length}）</summary><ul>{preview.issues.map((x, i) => <li key={i}>{typeof x === 'string' ? x : JSON.stringify(x)}</li>)}</ul></details>}
            </section><div className="side-stack"><section className="card"><div className="card-heading compact"><h2>连接状态</h2><Icon name="link"/></div><div className="connection-row"><div><strong>{state.broker.name}</strong><small>{connected ? '最近一次检查通过' : '等待连接检查'}</small></div><i className={cls('status-dot', connected && 'on')}/></div><Button className="full" disabled={busy || !state.actions?.status?.enabled} title={state.actions?.status?.message} onClick={() => run('status')}>{connected ? '重新检查连接' : '检查账户连接'}</Button>{account?.status === 'blocked' && <p className="inline-warning">{account.message}</p>}<div className="connection-row divider"><div><strong>{state.sources.find(s => s.id === state.source)?.name} 数据</strong><small>{selectedData?.configured === false ? '请先完成模块配置' : snapshot ? '已有本地快照' : '尚未下载数据'}</small></div><button className="icon-button" aria-label="打开数据中心" onClick={() => navigate('data')}><Icon name="arrow"/></button></div></section>
              <section className="card"><div className="card-heading compact"><h2>最近操作</h2><button className="text-link" onClick={() => navigate('logs')}>全部</button></div>{state.jobs.length ? <div className="activity">{state.jobs.slice(0, 3).map(job => <div key={job.id}><span className="activity-dot"/><div><strong>{actions[job.action] || job.action}</strong><small>{statuses[job.status] || job.status} · {time(job.started_at)}</small></div></div>)}</div> : <p className="small muted">完成操作后，记录会显示在这里。</p>}</section>
            </div></div>
          </>}
          {page === 'strategies' && <StrategyPage key={state.config_version} state={state} busy={busy} act={act}/>}
          {page === 'data' && <DataPage key={state.source} state={state} busy={busy} act={act} run={run} chooseSource={chooseSource} upload={upload} selectedFile={selectedFile}/>}
          {page === 'broker' && <BrokerPage state={state} busy={busy} run={run} chooseBroker={chooseBroker} authorize={authorizeBroker} bind={bindBroker} disconnect={disconnectBroker}/> }
          {page === 'positions' && <PositionsPage state={state} busy={busy} run={run}/>}
          {page === 'backtests' && <BacktestPage state={state} busy={busy} act={act}/>}
          {page === 'logs' && <><AutomationCard state={state} online={online} runtimeRunning={runtime?.status === 'running'} pending={pending} busy={busy} act={act}/><div className="section-toolbar"><h2>操作记录</h2><div className="button-row">{!access?.enabled && <Button disabled={busy} icon="power" onClick={() => setDialog('stop')}>退出整个工作台</Button>}</div></div><section className="card logs-card">{state.jobs.length === 0 ? <Empty title="暂无运行记录" icon="list">更新数据、预览和连接检查都会留下记录。</Empty> : state.jobs.map(job => <details className="job" key={job.id}><summary><span className="job-icon"><Icon name={job.status === 'running' ? 'clock' : 'list'}/></span><div><strong>{actions[job.action] || job.action}</strong><small>{time(job.started_at)} · {job.source_name || job.source} · {job.broker_name || job.broker || '历史记录'}</small></div><Tag tone={['blocked', 'paused', 'interrupted'].includes(job.status) ? 'amber' : job.status === 'running' ? 'blue' : 'muted'}>{statuses[job.status] || job.status}</Tag></summary><div className="job-body"><p>{job.message}</p>{job.action === 'preflight' && <PreflightProgress job={job} onNavigate={navigate}/>}{job.result?.issues?.length > 0 && <ul>{job.result.issues.map((x, i) => <li key={i}>{typeof x === 'string' ? x : JSON.stringify(x)}</li>)}</ul>}{job.events?.map((event, i) => <div className="log-line" key={i}><time>{time(event.at)}</time><span>{event.message}</span></div>)}<span className="muted small">记录编号：{job.id}</span></div></details>)}</section></>}
        </>}
        <footer className="page-footer"><span>Trading Bot · {access.enabled ? '云端工作台' : '本地工作台'}</span></footer>
      </main>
    </div>
    {dialog && <div className="modal-backdrop" onClick={() => setDialog(null)}><div className="modal" role="dialog" aria-modal="true" aria-labelledby="dialog-title" onClick={e => e.stopPropagation()}><h2 id="dialog-title">{dialog === 'execute' ? '确认提交模拟订单' : '退出整个工作台'}</h2><p>{dialog === 'execute' ? `使用 ${state.broker.name}（${state.broker.mode_label}），按刚才的 ${preview?.orders?.length || 0} 笔委托执行一轮。系统将再次检查模块、配置和行情时效。` : '退出会同时关闭面板服务和交易后台。只想停止交易后台，请使用右上角开关。'}</p><div className="button-row"><Button autoFocus onClick={() => setDialog(null)}>取消</Button><Button primary onClick={confirm}>{dialog === 'execute' ? '确认提交模拟订单' : '退出工作台'}</Button></div></div></div>}
  </div>;
}

function StrategyPage({ state, busy, act }) {
  const [strategy, setStrategy] = useState({ ...state.strategy });
  const selected = state.strategies.find(x => x.type === strategy.type);
  async function save(e) {
    e.preventDefault();
    await act(async () => {
      const value = { ...strategy };
      for (const field of selected.fields) if (field.type === 'json' && typeof value[field.name] === 'string') value[field.name] = JSON.parse(value[field.name]);
      return api('strategy', { strategy: value, version: state.config_version });
    });
  }
  return <><div className="strategy-grid">{state.strategies.filter(item => item.enabled).map(item => <article key={item.type} className={cls('strategy-option', item.type === strategy.type && 'selected')}><button type="button" className="strategy-select" aria-pressed={item.type === strategy.type} disabled={busy || !item.enabled} onClick={() => setStrategy({ type: item.type, ...item.defaults })}><div className="strategy-option-top"><Icon name="layers"/><Tag tone={item.type === state.strategy.type ? 'blue' : 'muted'}>{item.type === state.strategy.type ? '当前使用' : item.enabled ? '可替换' : '接口不兼容'}</Tag></div><h2>{item.name}</h2>{item.reason && <p>{item.reason}</p>}<small>v{item.version} · {item.type}</small></button><StrategyReadme content={item.readme?.content} filename={item.readme?.path} issue={item.readme_issue}/></article>)}</div><section className="card"><div className="card-heading"><div><h2>{selected?.name} · 参数</h2><p>参数表由策略模块提供；保存后需要重新预览。</p></div></div><form onSubmit={save}><div className="form-grid"><ModuleFields fields={selected?.fields || []} values={strategy} disabled={busy} onChange={(key, value) => setStrategy({ ...strategy, [key]: value })}/></div><div className="form-footer"><span className="muted small">框架限制：总仓位 ≤ {percent(state.limits.max_gross)}，单股 ≤ {percent(state.limits.max_weight)}</span><Button type="submit" primary disabled={busy || !selected?.enabled}>保存策略配置</Button></div></form></section><details className="details"><summary>其他已登记策略</summary><ul>{state.strategies.filter(item => !item.enabled).map(item => <li key={item.type}>{item.name} · {item.reason}<StrategyReadme content={item.readme?.content} filename={item.readme?.path} issue={item.readme_issue}/></li>)}</ul></details></>;
}

function DataPage({ state, busy, act, run, chooseSource, upload, selectedFile }) {
  const source = state.sources.find(s => s.id === state.source), snap = state.snapshot;
  const can = capability => source.capabilities?.includes(capability);
  const [settings, setSettings] = useState(() => Object.fromEntries((source.fields || []).map(f => [f.name, f.secret ? '' : source[f.name] ?? f.default ?? ''])));
  async function save(e) { e.preventDefault(); const values = { ...settings }; setSettings(Object.fromEntries(Object.entries(settings).map(([key, value]) => [key, source.fields.find(f => f.name === key)?.secret ? '' : value]))); await act(() => { for (const field of source.fields || []) if (field.type === 'json' && typeof values[field.name] === 'string') values[field.name] = JSON.parse(values[field.name]); return api('data/settings', values); }); }
  return <><section className="card"><div className="card-heading"><div><h2>数据模块</h2><p>接口和设置由模块声明，数据来源与账户模块独立。</p></div><Tag>{source.name}</Tag></div><div className="data-source-row"><label>当前数据源<select aria-label="数据中心数据源" value={state.source} disabled={busy} onChange={e => chooseSource(e.target.value)}>{state.sources.map(s => <option key={s.id} value={s.id} disabled={!s.enabled} title={s.reason}>{s.name}{!s.enabled && ' · 不可用'}</option>)}</select></label><div><strong>{source.name}</strong><p className="muted small">{source.message || source.description}</p></div></div>{source.issue && <p className="inline-warning">{source.issue}</p>}{state.compatibility_issue && <p className="inline-warning">{state.compatibility_issue}</p>}
    {state.data_requirements && <div className="info-grid"><div><span>当前策略的数据范围</span><strong>{state.data_requirements.universe_label}</strong></div><div><span>日线历史</span><strong>{state.data_requirements.price_bars} 个交易日</strong></div><div><span>按需读取字段</span><strong>{state.data_requirements.field_labels.join('、')}</strong></div></div>}
    {can('configure') && <details className="settings" open={source.configured === false}><summary>{source.name} · 连接设置</summary><form onSubmit={save}><div className="form-grid"><ModuleFields fields={source.fields || []} values={settings} disabled={busy} onChange={(key, value) => setSettings({ ...settings, [key]: value })}/></div><div className="card-actions"><Button type="submit" disabled={busy}>保存设置</Button><span className="small muted">密钥只保存在本机，不返回页面。</span></div></form></details>}
    <div className="card-actions">{can('collect') && <Button primary icon="refresh" disabled={busy || !state.actions?.refresh?.enabled} title={state.actions?.refresh?.message} onClick={() => run('refresh')}>下载并预览</Button>}{can('inspect') && <Button disabled={busy || !state.actions?.['data-status']?.enabled} title={state.actions?.['data-status']?.message} onClick={() => run('data-status')}>检查数据权限</Button>}{can('upload') && <Button icon="upload" disabled={busy} onClick={() => selectedFile.current?.click()}>导入快照</Button>}<input type="file" accept=".json,application/json" ref={selectedFile} hidden onChange={e => { upload(e.target.files?.[0]); e.target.value = ''; }}/></div></section>
    <section className="card"><div className="card-heading"><h2>当前快照</h2>{snap && <Tag tone="blue">{snap.research_only ? '仅供研究' : snap.synthetic ? '合成演示' : '待执行校验'}</Tag>}</div>{!snap ? <Empty title="这个数据模块还没有快照" icon="database">通过模块支持的下载或文件输入方式准备数据。</Empty> : <><div className="info-grid"><div><span>价格日期</span><strong>{snap.price_as_of || '—'}</strong></div><div><span>有效 / 候选股票</span><strong>{number(snap.usable_count)} / {number(snap.candidate_count)}</strong></div><div><span>采集完成时间</span><strong>{time(snap.collected_at)}</strong></div></div><p className="small muted">{snap.collection_complete === false ? '采集未完整覆盖候选股票；请结合缺失范围判断选股结果。' : snap.collection_complete === true ? '采集流程已完成，仍需检查数据说明。' : '文件未提供完整采集状态。'}</p>{snap.limitations?.length > 0 && <details className="details"><summary>数据说明与限制</summary><ul>{snap.limitations.map((item, i) => <li key={i}>{typeof item === 'string' ? item : JSON.stringify(item)}</li>)}</ul></details>}<Button disabled={busy || !state.actions?.preview?.enabled} title={state.actions?.preview?.message} onClick={() => run('preview')}>用此快照预览策略</Button></>}</section>
    <ModuleIssues items={state.sources}/><details className="details"><summary>所有已登记的数据接口</summary><ul>{state.inventory.data.map(item => <li key={item.type}>{item.name} · {item.kind}{!item.has_control && ' · 尚未接入工作台控制接口'}</li>)}</ul></details></>;
}

function BrokerPage({ state, busy, run, chooseBroker, authorize, bind, disconnect }) {
  const broker = state.broker, account = state.account, auth = state.broker_authorization;
  const loginMode = broker.authentication?.mode;
  return <>
    <section className="card">
      <div className="card-heading"><div><h2>当前账户模块</h2><p>切换后重新核验账户，旧订单预览失效。</p></div><Tag tone={state.broker_verified ? 'blue' : 'amber'}>{state.broker_verified ? '已核验' : '待核验'}</Tag></div>
      <div className="data-source-row"><label>券商模块<select aria-label="券商模块" value={broker.id} disabled={busy} onChange={e => chooseBroker(e.target.value)}>{state.brokers.map(item => <option key={item.id} value={item.id} disabled={!item.enabled}>{item.name}{!item.enabled && ' · 未启用'}</option>)}</select></label><div><strong>{broker.name}</strong><p className="small muted">{broker.description}</p></div></div>
      {loginMode === 'oauth' ? <div className="workflow authorization-panel">
        <div><strong>{broker.authentication.title}</strong><p className="small muted">{auth?.notice || auth?.note || '通过券商官网完成授权，密码不交给本程序。'}</p></div>
        <Button disabled={busy} onClick={authorize}>{auth?.authorized ? '重新网页授权' : '前往券商官网授权'}</Button>
        {auth?.authorized && <label>美股模拟账户<select aria-label="网页模拟账户" disabled={busy} value={auth.selected || ''} onChange={e => e.target.value && bind(e.target.value)}><option value="" disabled>请选择账户</option>{auth.accounts.map(item => <option key={item.id} value={item.id}>{item.name} · {item.account_hint}</option>)}</select></label>}
      </div> : <p className="small muted">{broker.authentication?.title}。{loginMode === 'external' ? '请先在官方客户端或当前模块的 API 配置中登录，然后检查账户连接。' : '使用本地回放账户。'}</p>}
      <div className="info-grid"><div><span>账户环境</span><strong>{broker.mode_label}</strong></div><div><span>模块版本</span><strong>{broker.version}</strong></div><div><span>最近检查</span><strong>{time(account?.checked_at)}</strong></div></div>
      <div className="card-actions">{broker.capabilities.includes('inspect') && <><Button primary disabled={busy || !state.actions?.status?.enabled} title={state.actions?.status?.message} onClick={() => run('status')}>检查账户连接</Button><Button disabled={busy || !state.actions?.offline?.enabled} onClick={() => run('offline')}>检查本地配置</Button></>}<Button disabled={busy} onClick={disconnect}>断开当前账户</Button></div>
      {account && <><p className="result-message">{account.message}</p>{account.details?.issues?.length > 0 && <ul className="small muted">{account.details.issues.map((item, i) => <li key={i}>{typeof item === 'string' ? item : JSON.stringify(item)}</li>)}</ul>}</>}
    </section>
    <ModuleIssues items={state.brokers}/><section className="card quiet-card"><h2>独立的账户状态</h2><p>当前程序只核验所选模块。切换时清除网页授权和旧账户预览；仍有待核对委托时先处理订单。</p><p>断开仅停用本程序的账户访问。官方客户端登录或券商端授权，需要在对应平台退出或撤销。当前工作台仅开放模拟与本地回放。</p><details className="details"><summary>所有已登记的账户接口</summary><ul>{state.inventory.broker.map(item => <li key={item.type}>{item.name} · {item.kind}{!item.has_control && ' · 尚未接入工作台控制接口'}</li>)}</ul></details></section>
  </>;
}

function PositionsPage({ state, busy, run }) {
  const [orders, setOrders] = useState({ items: [], message: '正在读取本地账本…' });
  useEffect(() => { let live = true; api('orders').then(value => { if (live) setOrders(value); }).catch(e => { if (live) setOrders({ items: [], message: e.message }); }); return () => { live = false; }; }, [state.broker.id, state.jobs[0]?.id, state.jobs[0]?.status]);
  const account = state.account, details = account?.status === 'connected' ? account.details : null;
  return <><section className="card"><div className="card-heading"><div><h2>{state.broker.name} · 持仓</h2><p>{details ? `账户检查时间：${time(account.checked_at)}` : '先检查账户连接，读取券商返回的持仓。'}</p></div><Button disabled={busy || !state.actions?.status?.enabled} title={state.actions?.status?.message} onClick={() => run('status')}>刷新账户</Button></div>{details && <div className="mini-metrics"><span>现金 <strong>{details.currency || ''} {number(details.cash)}</strong></span><span>未完成委托 <strong>{details.open_order_count ?? '—'}</strong></span></div>}{!details?.positions?.length ? <Empty title={details ? '当前账户没有持仓' : '尚无持仓数据'} icon="briefcase">{details ? '账户返回了空持仓列表。' : '这里不会把未知状态显示成零持仓。'}</Empty> : <div className="table-wrap"><table><thead><tr><th>股票</th><th className="right">持有数量</th></tr></thead><tbody>{details.positions.map(p => <tr key={p.symbol}><td><strong>{p.symbol}</strong></td><td className="right mono">{number(p.quantity)}</td></tr>)}</tbody></table></div>}</section><section className="card"><div className="card-heading"><div><h2>本程序的订单记录</h2><p>{orders.message}</p></div></div>{!orders.items.length ? <Empty title="暂无可显示的订单" icon="list">交易后可在这里核对委托与成交记录。</Empty> : <div className="table-wrap"><table><thead><tr><th>股票</th><th>方向</th><th>状态</th><th className="right">委托数量</th><th className="right">已成交</th><th className="right">均价</th></tr></thead><tbody>{orders.items.map(o => <tr key={o.id}><td>{o.symbol}</td><td>{orderSide(o.side)}</td><td>{orderStatus(o.status)}</td><td className="right">{number(o.quantity)}</td><td className="right">{number(o.filled_quantity)}</td><td className="right">{number(o.average_price)}</td></tr>)}</tbody></table></div>}</section></>;
}

function BacktestPage({ state, busy, act }) {
  const cfg = state.backtest_config || {}, [start, setStart] = useState(cfg.start || ''), [end, setEnd] = useState(cfg.end || '');
  const [slip, setSlip] = useState(cfg.slippage_bps ?? 10), [items, setItems] = useState([]), [loadError, setLoadError] = useState('');
  const [selected, setSelected] = useState([]), [comparison, setComparison] = useState(null);
  const [note, setNote] = useState(''), [importId, setImportId] = useState(null), input = useRef(null);
  async function load() { try { setItems((await api('experiments')).items); setLoadError(''); } catch (e) { setLoadError(e.message); } }
  useEffect(() => { load(); }, []);
  async function exportCode(e) { e.preventDefault(); const result = await act(() => api('experiments', { start, end, slippage_bps: slip, note })); if (result) { await load(); await act(() => downloadCode(result.id), '代码已准备下载。复制到 QuantConnect 后运行回测。'); } }
  async function importFile(file) { if (!file || !importId) return; const result = await act(() => api(`experiments/${importId}/result`, file, { raw: true })); if (result) await load(); }
  const completions = { reported_complete: '报告显示完成', unverified: '完成状态未核验', incomplete: '未完成', failed: '运行失败' };
  return <><section className="card"><div className="card-heading"><div><h2>创建回测实验</h2><p>使用当前策略：{state.strategy_name}。这里导出代码，回测由 QuantConnect 执行。</p></div><Tag>QuantConnect</Tag></div><form onSubmit={exportCode}><div className="backtest-form"><label>开始日期<input type="date" required value={start} disabled={busy} onChange={e => setStart(e.target.value)}/></label><label>结束日期<input type="date" required value={end} disabled={busy} onChange={e => setEnd(e.target.value)}/></label><label>滑点 (bps)<input type="number" min="0" max="9999" required value={slip} disabled={busy} onChange={e => setSlip(Number(e.target.value))}/></label><Button primary type="submit" disabled={busy}>导出回测代码</Button></div><details className="details"><summary>实验备注</summary><textarea value={note} onChange={e => setNote(e.target.value)} maxLength={2000} placeholder="这次想验证什么？"/></details></form></section>
    <div className="section-toolbar"><h2>实验记录</h2><Button disabled={busy || selected.length < 2} onClick={async () => { const result = await act(() => api('compare', { ids: selected })); if (result) setComparison(result); }}>比较已选结果 ({selected.length})</Button></div>{loadError && <div className="alert error">{loadError}</div>}{!items.length ? <section className="card"><Empty title="还没有通过面板创建的实验" icon="chart">导出代码后，这里会保留参数与版本。已有命令行实验继续保存在原目录。</Empty></section> : items.map(item => <section className="card experiment" key={item.id}><div className="card-heading"><div><h2>{item.invalid ? '记录校验失败' : `${item.configuration.backtest.start} — ${item.configuration.backtest.end}`}</h2><p>{item.id}{!item.invalid && ` · 滑点 ${item.configuration.backtest.slippage_bps} bps`}</p></div><div className="button-row"><Button disabled={busy || item.invalid} onClick={() => act(() => downloadCode(item.id), '代码已准备下载。')}>下载代码</Button><Button disabled={busy || item.invalid} onClick={() => { setImportId(item.id); input.current?.click(); }}>导入结果 JSON</Button></div></div>{item.note && <p className="small muted">{item.note}</p>}{!item.results.length ? <p className="small muted">仅导出，尚无回测结果。</p> : item.results.map(result => result.invalid ? <p key={result.id} className="inline-warning">结果证据校验失败，已禁止比较。</p> : <div className="backtest-result" key={result.id}><label className="result-select"><input type="checkbox" checked={selected.includes(result.id)} onChange={e => setSelected(e.target.checked ? [...selected, result.id] : selected.filter(id => id !== result.id))}/><span>{completions[result.completion]} · {result.code_evidence === 'source_unverified' ? '代码来源未核验' : '代码副本匹配'}</span></label><div className="result-metrics"><div><span>总收益</span><strong>{percent(result.metrics.total_return)}</strong></div><div><span>年化收益</span><strong>{percent(result.metrics.annual_return)}</strong></div><div><span>最大回撤</span><strong>{percent(result.metrics.max_drawdown)}</strong></div><div><span>Sharpe</span><strong>{number(result.metrics.sharpe)}</strong></div><div><span>订单数</span><strong>{number(result.metrics.total_orders)}</strong></div></div></div>)}</section>)}<input hidden ref={input} type="file" accept=".json,application/json" onChange={e => { importFile(e.target.files?.[0]); e.target.value = ''; }}/>
    {comparison && <section className="card"><div className="card-heading"><div><h2>结果比较</h2><p>{comparison.comparison_differences.length ? `测试条件存在差异：${comparison.comparison_differences.join('、')}` : '区间、初始资金和成本设置相同。'} 比较不会自动批准实盘。</p></div></div><div className="table-wrap"><table><thead><tr><th>实验</th><th>总收益</th><th>年化收益</th><th>回撤</th><th>Sharpe</th></tr></thead><tbody>{comparison.rows.map((row, i) => <tr key={i}><td>{row.experiment}</td><td>{percent(row.total_return)}</td><td>{percent(row.annual_return)}</td><td>{percent(row.max_drawdown)}</td><td>{number(row.sharpe)}</td></tr>)}</tbody></table></div></section>}
  </>;
}

createRoot(document.getElementById('root')).render(<PortalAccess>{(access, logout) => <App access={access} onLogout={logout}/>}</PortalAccess>);
