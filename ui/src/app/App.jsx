import React, { Suspense, lazy, useEffect, useRef, useState } from 'react';
import { api } from '../api';
import { runtimeView } from '../runtime';
import { isServerDeployment } from '../server';
import { actions, number } from '../format';
import { currencyLabel, environmentLabel, executionContext } from '../execution_context';
import { orderSummary } from '../orders';
import { Button, Empty, Icon } from '../ui';
import { usePortalSession } from './usePortalSession';
import { usePortalActions } from './usePortalActions';
import { resolveDestination, routeFromHash, routeHash } from './navigation';
import { PortalShell } from './PortalShell';
import { reloadOnceForStaleBuild } from './stale_build';
import { Dialog } from '../components/Dialog';
import { OverviewPage } from '../pages/OverviewPage';

const lazyPage = (loader, name) => lazy(() => loader().then(module => ({ default: module[name] }), error => reloadOnceForStaleBuild(error)));

class PageBoundary extends React.Component {
  constructor(props) { super(props); this.state = { failed: false }; }
  static getDerivedStateFromError() { return { failed: true }; }
  componentDidUpdate(previous) { if (this.state.failed && previous.resetKey !== this.props.resetKey) this.setState({ failed: false }); }
  render() {
    if (!this.state.failed) return this.props.children;
    return <section className="runtime-empty"><Empty title="页面没有加载成功" icon="refresh">程序可能刚刚升级，或网络短暂中断。刷新后会载入最新页面；后台运行不受影响。</Empty>
      <Button primary icon="refresh" onClick={() => window.location.reload()}>刷新页面</Button></section>;
  }
}
const ManualRun = lazyPage(() => import('../pages/ManualRun'), 'ManualRun');
const StrategyWorkspace = lazyPage(() => import('../pages/StrategyWorkspace'), 'StrategyWorkspace');
const DataPage = lazyPage(() => import('../pages/ModuleSettings'), 'DataPage');
const BrokerPage = lazyPage(() => import('../pages/ModuleSettings'), 'BrokerPage');
const PositionsPage = lazyPage(() => import('../pages/AccountPage'), 'PositionsPage');
const SystemPage = lazyPage(() => import('../pages/SystemPage'), 'SystemPage');
const StrategyRuns = lazyPage(() => import('../components/StrategyRuns'), 'StrategyRuns');
const AutomationCard = lazyPage(() => import('../components/AutomationCard'), 'AutomationCard');
const PerformancePanel = lazyPage(() => import('../components/PerformancePanel'), 'PerformancePanel');
const BacktestsPage = lazyPage(() => import('../pages/BacktestsPage'), 'BacktestsPage');

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
  // Routine background jobs (the account monitor's periodic observation) must not
  // turn the code editor read-only or lock forms; only user-started jobs do.
  const editorBusy = pending || (!!state?.active && state.active.background !== true) || !online || stopped || runtime?.status !== 'running';
  const runtimeStatus = runtimeView(runtime, online && !stopped, pending, state?.active, state?.automation);

  useEffect(() => {
    // Confirmations fade on their own; errors stay until dismissed or replaced.
    if (!notice) return;
    const id = setTimeout(() => setNotice(''), 8000);
    return () => clearTimeout(id);
  }, [notice]);

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
    // Modules without an order-preparation step (the local replay account) size
    // orders at submission time; the preview then has targets but no orders.
    const orders = Array.isArray(state.preview?.orders) ? state.preview.orders : null;
    setDialog({ kind: 'execute', brokerId: state.broker.id, brokerName: state.broker.name, environment: environmentLabel(state.broker), currency: currencyLabel(executionContext(state.broker).currency), previewAt: state.preview?.created_at, count: orders ? orders.length : null, targets: state.preview?.targets?.length || 0, totals: orderSummary(orders || [], state.preview?.execution_context) });
  }
  function requestDisconnect() {
    setDialog({ kind: 'disconnect', brokerName: state.broker.name, planRunning: state.automation?.enabled === true, monitoring: state.monitoring?.settings?.enabled !== false });
  }
  const confirmChanged = dialog?.kind === 'execute' && (!state || dialog.brokerId !== state.broker.id || dialog.previewAt !== state.preview?.created_at || !state.actions?.execute?.enabled);
  async function confirm() {
    if (pending || confirmChanged) return;
    const current = dialog; setDialog(null);
    if (current.kind === 'execute') await act(() => api('jobs', { action: 'execute', confirmation: current.brokerId }));
    else if (current.kind === 'disconnect') await act(() => api('broker/disconnect', {}));
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
    if (route.page === 'overview') return <OverviewPage state={state} runtime={runtime} runtimeStatus={runtimeStatus} navigate={navigate}/>;
    if (route.page === 'backtests') return <BacktestsPage {...common} route={route} navigate={navigate}/>;
    if (route.page === 'runs') {
      if (route.view === 'manual') return <><ContextSelection state={state} busy={busy} chooseBroker={chooseBroker} navigate={navigate}/><ManualRun {...common} navigate={navigate} chooseSource={chooseSource} submit={requestSubmission}/></>;
      if (route.view === 'plan') return <><ContextSelection state={state} busy={busy} chooseBroker={chooseBroker} navigate={navigate}/><AutomationCard {...common} online={online} runtimeRunning={runtime?.status === 'running'} pending={pending} chooseBroker={chooseBroker}/></>;
      return <StrategyRuns {...common} pending={pending} route={route} onNavigate={navigate}/>;
    }
    if (route.page === 'account') {
      // The account page only views records. Switching the viewed account is a
      // route parameter; the operating account (previews, authorization, the
      // single-strategy plan binding) stays untouched.
      const viewed = state.brokers.some(item => item.id === route.broker) ? route.broker : state.broker.id;
      const viewBroker = id => navigate({ page: 'account', view: route.view, broker: id });
      if (route.view === 'performance') return <PerformancePanel key={viewed} {...common} brokerId={viewed} viewBroker={viewBroker} chooseBroker={chooseBroker} navigate={navigate}/>;
      return <><div className="context-toolbar"><label>查看账户<select value={viewed} onChange={event => viewBroker(event.target.value)}>{state.brokers.map(item => <option key={item.id} value={item.id} disabled={!item.enabled}>{item.name}</option>)}</select></label>{viewed !== state.broker.id && <span className="small muted">操作账户仍是 {state.broker.name}；这里只查看记录。</span>}<button className="text-link" onClick={() => navigate('broker')}>连接设置</button></div><PositionsPage key={viewed} {...common} brokerId={viewed} chooseBroker={chooseBroker}/></>;
    }
    if (route.page === 'strategies') return <StrategyWorkspace {...common} editorBusy={editorBusy} route={route} navigate={navigate}/>;
    if (route.page === 'connections') {
      if (route.view === 'data') return <DataPage key={state.source} {...common} chooseSource={chooseSource} upload={upload} selectedFile={selectedFile}/>;
      if (route.view === 'broker') return <BrokerPage {...common} chooseBroker={chooseBroker} authorize={authorize} bind={account => act(() => api('broker-auth/bind', { account }))} disconnect={requestDisconnect}/>;
    }
    return <SystemPage view={route.view} incident={route.incident} {...common} navigate={navigate} access={access} shutdown={() => setDialog({ kind: 'stop' })} refresh={refresh}/>;
  }

  // The page header (title, one-line purpose, the single main action) is owned by
  // the shell; only the "运行" section changes it with the route.
  function pageHeader() {
    if (!state || stopped || runtime?.status !== 'running' || route.page !== 'runs') return null;
    const manager = state.strategy_manager;
    const backToList = { back: '返回运行列表', onBack: () => navigate('instances') };
    if (route.view === 'manual') return { ...backToList, title: '手动调仓 · 只跑一次', intro: '不建实例，用当前策略、账户和数据源手动跑一轮；适合试一次。' };
    if (route.view === 'plan') return { ...backToList, title: '旧版单策略计划', intro: '仍可使用的兼容方式；新的定时运行请用「新建运行」。' };
    if (route.instance) {
      const item = manager?.instances?.find(entry => entry.id === route.instance);
      const strategyName = state.strategies.find(entry => entry.type === item?.strategy?.type)?.name || item?.strategy?.type;
      const brokerName = state.brokers.find(entry => entry.id === item?.broker_id)?.name || item?.broker_id;
      return { ...backToList, title: item?.name || '运行详情', intro: item ? `${strategyName} · ${brokerName}` : '' };
    }
    if (route.create) return { ...backToList, title: '新建运行', intro: '选择策略、账户、数据源、资金比例和计划；保存后再确认启动。' };
    return { action: <Button primary icon="plus" disabled={busy || manager?.corrupt} onClick={() => navigate({ page: 'runs', view: 'instances', create: true })}>新建运行</Button> };
  }

  return <PortalShell route={route} navigate={navigate} access={access} onLogout={onLogout} session={session} runtimeStatus={runtimeStatus} toggleRuntime={toggleRuntime} header={pageHeader()}>
    {!stopped && (error || session.connectionError) && <div className="alert error" role="alert"><span>{error || session.connectionError}{incidentOf(error) && <> <button className="text-link" onClick={() => navigate({ page: 'system', view: 'logs', incident: incidentOf(error) })}>查看日志</button></>}</span>{error && <button onClick={() => setError('')} aria-label="关闭错误"><Icon name="close" size={16}/></button>}</div>}
    {!stopped && notice && <div className="alert success" role="status"><span>{notice}</span><button onClick={() => setNotice('')} aria-label="关闭提示"><Icon name="close" size={16}/></button></div>}
    {state?.module_issue && route.page !== 'overview' && <div className="alert error" role="alert">{state.module_issue}</div>}
    {state?.active && <div className="running-strip" role="status"><div><strong>{actions[state.active.action] || state.active.action}</strong><span>{state.active.message}</span></div>{state.active.cancel_available && <Button disabled={pending} onClick={() => act(() => api('cancel-download', {}))}>停止下载</Button>}</div>}
    <PageBoundary resetKey={routeHash(route)}><Suspense fallback={<div className="loading-state" role="status"><div/><div/><p>正在打开页面…</p></div>}>{content()}</Suspense></PageBoundary>
    {dialog && <Dialog labelledBy="confirm-title" canDismiss={!pending} onDismiss={() => setDialog(null)}>
      <h2 id="confirm-title">{dialog.kind === 'execute' ? `确认提交订单 · ${dialog.environment}` : dialog.kind === 'disconnect' ? `断开当前账户 · ${dialog.brokerName}` : '退出整个工作台'}</h2>
      {dialog.kind === 'execute' && dialog.count !== null && <div className="order-summary">
        <div><span>委托笔数</span><strong>{dialog.count}</strong></div>
        <div><span>卖出 · {dialog.totals.sellCount} 笔</span><strong className="loss">{number(dialog.totals.sell)}</strong></div>
        <div><span>买入 · {dialog.totals.buyCount} 笔</span><strong className="gain">{number(dialog.totals.buy)}</strong></div>
      </div>}
      {dialog.kind === 'execute' && dialog.count === null && <p className="inline-warning">这个账户模块在提交时才按目标权重和当前持仓计算每笔委托，预览中没有逐笔委托可核对。本次共 {dialog.targets} 个目标标的，将按快照报价成交。</p>}
      {dialog.kind === 'disconnect' && <ul>
        <li>本程序停用对 {dialog.brokerName} 的访问；官方客户端登录和券商端授权需要另行退出或撤销。</li>
        {dialog.planRunning && <li>正在运行的单策略计划会被关闭；已提交的委托仍可能成交，不会自动撤单。</li>}
        {dialog.monitoring && <li>账户定时监控会同时关闭，之后可在「系统 → 监控与通知」重新开启。</li>}
        <li>旧的订单预览与网页授权会被清除，重新连接后需再次检查账户。</li>
      </ul>}
      <p>{dialog.kind === 'execute' ? (dialog.count === null
        ? `使用 ${dialog.brokerName} 向 ${dialog.environment} 按目标权重提交委托（以 ${dialog.currency} 计价，先卖后买）。执行前将再次检查账户、配置和行情。`
        : `使用 ${dialog.brokerName} 向 ${dialog.environment} 提交以上委托（金额以 ${dialog.currency} 限价估算，先卖后买）。执行前将再次检查账户、配置和行情。`)
        : dialog.kind === 'disconnect' ? '确认后立即生效；账本和历史记录会保留。' : '退出会同时关闭工作台和交易后台。已提交的委托仍可能成交。'}</p>
      {confirmChanged && <p className="inline-warning">账户或订单预览已变化，请关闭并重新核对。</p>}
      <div className="button-row"><Button autoFocus disabled={pending} onClick={() => setDialog(null)}>取消</Button><Button primary disabled={pending || confirmChanged || (dialog.kind === 'execute' && busy)} onClick={confirm}>确认{dialog.kind === 'execute' ? '提交订单' : dialog.kind === 'disconnect' ? '断开' : '退出'}</Button></div>
    </Dialog>}
  </PortalShell>;
}

// Backend error messages end with "（诊断编号 X）"; the id finds the record in the logs.
function incidentOf(message) {
  const match = /诊断编号\s*([0-9a-f]{6,40})/.exec(message || '');
  return match ? match[1] : '';
}

function ContextSelection({ state, busy, chooseBroker, navigate }) {
  return <div className="context-toolbar">
    <label>操作账户<select value={state.broker.id} disabled={busy} onChange={event => chooseBroker(event.target.value)}>{state.brokers.map(item => <option key={item.id} value={item.id} disabled={!item.enabled}>{item.name}</option>)}</select></label>
    <div><span className="small muted">手动配置 · {state.strategy_name}</span><button className="text-link" onClick={() => navigate({ page: 'strategies', strategy: state.strategy.type, tab: 'parameters' })}>修改策略参数</button></div>
  </div>;
}
