import React, { Suspense, lazy, useEffect, useRef, useState } from 'react';
import { api } from '../api';
import { runtimeView } from '../runtime';
import { isServerDeployment } from '../server';
import { actions } from '../format';
import { Button, Empty, Icon } from '../ui';
import { usePortalSession } from './usePortalSession';
import { usePortalActions } from './usePortalActions';
import { resolveDestination, routeFromHash, routeHash } from './navigation';
import { PortalShell } from './PortalShell';
import { Dialog } from '../components/Dialog';
import { OverviewPage } from '../pages/OverviewPage';

const lazyPage = (loader, name) => lazy(() => loader().then(module => ({ default: module[name] })));
const ManualRun = lazyPage(() => import('../pages/ManualRun'), 'ManualRun');
const StrategyWorkspace = lazyPage(() => import('../pages/StrategyWorkspace'), 'StrategyWorkspace');
const DataPage = lazyPage(() => import('../pages/ModuleSettings'), 'DataPage');
const BrokerPage = lazyPage(() => import('../pages/ModuleSettings'), 'BrokerPage');
const PositionsPage = lazyPage(() => import('../pages/AccountPage'), 'PositionsPage');
const ResearchPage = lazyPage(() => import('../pages/ResearchPage'), 'ResearchPage');
const SystemPage = lazyPage(() => import('../pages/SystemPage'), 'SystemPage');
const StrategyRuns = lazyPage(() => import('../components/StrategyRuns'), 'StrategyRuns');
const AutomationCard = lazyPage(() => import('../components/AutomationCard'), 'AutomationCard');
const PerformancePanel = lazyPage(() => import('../components/PerformancePanel'), 'PerformancePanel');

export function App({ access, onLogout }) {
  const server = isServerDeployment(access);
  const session = usePortalSession();
  const { state, runtime, online, stopped, refresh } = session;
  const operation = usePortalActions(session);
  const { act, run, pending, error, notice, setError, setNotice } = operation;
  const [route, setRoute] = useState(() => routeFromHash(window.location.hash, server));
  const [dialog, setDialog] = useState(null);
  const selectedFile = useRef(null);
  const busy = pending || !!state?.active || !online || stopped || runtime?.status !== 'running';
  const runtimeStatus = runtimeView(runtime, online && !stopped, pending, state?.active, state?.automation);

  useEffect(() => {
    const read = () => setRoute(routeFromHash(window.location.hash, server));
    window.addEventListener('hashchange', read);
    return () => window.removeEventListener('hashchange', read);
  }, [server]);

  function navigate(destination) {
    const next = resolveDestination(destination, server);
    setRoute(next); setNotice(''); setError('');
    window.location.hash = routeHash(next);
    window.scrollTo({ top: 0, behavior: 'auto' });
  }
  async function toggleRuntime() {
    if (runtimeStatus.disabled) return;
    await act(async () => ({ ...await api(runtimeStatus.status === 'running' ? 'runtime/stop' : 'runtime/start', {}), message: '' }));
  }
  const chooseSource = source => act(() => api('source', { source }));
  const chooseBroker = broker => act(() => api('broker', { broker }));
  async function authorize() {
    const result = await act(() => api('broker-auth/start', {}, { timeoutMs: 35000 }));
    if (result?.authorization_url) window.location.assign(result.authorization_url);
  }
  async function upload(file) {
    if (file) await act(() => api('snapshot', file, { raw: true }), '快照已导入，可在手动调仓中预览。');
  }
  function requestSubmission() {
    setDialog({ kind: 'execute', brokerId: state.broker.id, brokerName: state.broker.name, previewAt: state.preview?.created_at, count: state.preview?.orders?.length || 0 });
  }
  const confirmChanged = dialog?.kind === 'execute' && (!state || dialog.brokerId !== state.broker.id || dialog.previewAt !== state.preview?.created_at || !state.actions?.execute?.enabled);
  async function confirm() {
    if (pending || confirmChanged) return;
    const current = dialog; setDialog(null);
    if (current.kind === 'execute') await act(() => api('jobs', { action: 'execute', confirmation: current.brokerId }));
    else await act(async () => { await api('shutdown', {}); session.markStopped(); }, '', { refreshAfter: false });
  }

  function content() {
    if (route.page === 'system' && route.view === 'server' && server) return <SystemPage view="server" navigate={navigate} refresh={refresh}/>;
    if (stopped) return <Empty title="工作台已退出">再次双击 Trading Bot.exe 即可打开。</Empty>;
    if (runtime?.status !== 'running') return <section className="runtime-empty">
      <Empty title={runtimeStatus.label} icon="power">{runtimeStatus.status === 'stopped' ? '打开右上角交易后台开关后，可继续查看策略与账户。' : runtimeStatus.hint}</Empty>
      {route.page === 'system' && !server && <Button disabled={!online || pending || ['starting', 'stopping'].includes(runtime?.status)} onClick={() => setDialog({ kind: 'stop' })}>退出整个工作台</Button>}
    </section>;
    if (!state) return <div className="loading-state" role="status"><div/><div/><p>{session.connectionError ? '正在重新读取后台状态…' : '正在读取工作台…'}</p></div>;
    const common = { state, busy, act, run };
    if (route.page === 'overview') return <OverviewPage state={state} runtimeStatus={runtimeStatus} navigate={navigate}/>;
    if (route.page === 'runs') {
      if (route.view === 'manual') return <><ContextSelection state={state} busy={busy} chooseBroker={chooseBroker} navigate={navigate}/><ManualRun {...common} navigate={navigate} chooseSource={chooseSource} submit={requestSubmission}/></>;
      if (route.view === 'plan') return <><p className="page-intro">使用一套策略按计划运行。多份策略请在策略实例中管理。</p><ContextSelection state={state} busy={busy} chooseBroker={chooseBroker} navigate={navigate}/><AutomationCard {...common} online={online} runtimeRunning={runtime?.status === 'running'} pending={pending} chooseBroker={chooseBroker}/></>;
      return <StrategyRuns {...common} pending={pending} route={route} onNavigate={navigate}/>;
    }
    if (route.page === 'account') {
      if (route.view === 'performance') return <PerformancePanel key={state.broker.id} {...common} chooseBroker={chooseBroker} navigate={navigate}/>;
      return <><div className="context-toolbar"><label>查看账户<select value={state.broker.id} disabled={busy} onChange={event => chooseBroker(event.target.value)}>{state.brokers.map(item => <option key={item.id} value={item.id} disabled={!item.enabled}>{item.name}</option>)}</select></label><button className="text-link" onClick={() => navigate('broker')}>连接设置</button></div><PositionsPage key={state.broker.id} {...common}/></>;
    }
    if (route.page === 'modules') {
      if (route.view === 'data') return <DataPage key={state.source} {...common} chooseSource={chooseSource} upload={upload} selectedFile={selectedFile}/>;
      if (route.view === 'broker') return <BrokerPage {...common} chooseBroker={chooseBroker} authorize={authorize} bind={account => act(() => api('broker-auth/bind', { account }))} disconnect={() => act(() => api('broker/disconnect', {}))}/>;
      return <StrategyWorkspace {...common} route={route} navigate={navigate}/>;
    }
    if (route.page === 'research') return <ResearchPage {...common} navigate={navigate}/>;
    return <SystemPage view={route.view} {...common} navigate={navigate} access={access} shutdown={() => setDialog({ kind: 'stop' })} refresh={refresh}/>;
  }

  return <PortalShell route={route} navigate={navigate} access={access} onLogout={onLogout} session={session} runtimeStatus={runtimeStatus} toggleRuntime={toggleRuntime}>
    {!stopped && (error || session.connectionError) && <div className="alert error" role="alert"><span>{error || session.connectionError}</span>{error && <button onClick={() => setError('')} aria-label="关闭错误"><Icon name="close" size={16}/></button>}</div>}
    {!stopped && notice && <div className="alert" role="status"><span>{notice}</span><button onClick={() => setNotice('')} aria-label="关闭提示"><Icon name="close" size={16}/></button></div>}
    {state?.module_issue && route.page !== 'overview' && <div className="alert error" role="alert">{state.module_issue}</div>}
    {state?.active && <div className="running-strip" role="status"><div><strong>{actions[state.active.action] || state.active.action}</strong><span>{state.active.message}</span></div>{state.active.cancel_available && <Button disabled={pending} onClick={() => act(() => api('cancel-download', {}))}>停止下载</Button>}</div>}
    <Suspense fallback={<div className="loading-state" role="status"><div/><div/><p>正在打开页面…</p></div>}>{content()}</Suspense>
    {dialog && <Dialog labelledBy="confirm-title" canDismiss={!pending} onDismiss={() => setDialog(null)}>
      <h2 id="confirm-title">{dialog.kind === 'execute' ? '确认提交模拟订单' : '退出整个工作台'}</h2>
      <p>{dialog.kind === 'execute' ? `使用 ${dialog.brokerName}，提交刚才准备的 ${dialog.count} 笔模拟委托。执行前将再次检查账户、配置和行情。` : '退出会同时关闭工作台和交易后台。已提交的委托仍可能成交。'}</p>
      {confirmChanged && <p className="inline-warning">账户或订单预览已变化，请关闭并重新核对。</p>}
      <div className="button-row"><Button autoFocus disabled={pending} onClick={() => setDialog(null)}>取消</Button><Button primary disabled={pending || confirmChanged || (dialog.kind === 'execute' && busy)} onClick={confirm}>确认{dialog.kind === 'execute' ? '提交模拟订单' : '退出'}</Button></div>
    </Dialog>}
  </PortalShell>;
}

function ContextSelection({ state, busy, chooseBroker, navigate }) {
  return <div className="context-toolbar">
    <label>操作账户<select value={state.broker.id} disabled={busy} onChange={event => chooseBroker(event.target.value)}>{state.brokers.map(item => <option key={item.id} value={item.id} disabled={!item.enabled}>{item.name}</option>)}</select></label>
    <div><span className="small muted">手动配置 · {state.strategy_name}</span><button className="text-link" onClick={() => navigate('strategies')}>修改策略参数</button></div>
  </div>;
}
