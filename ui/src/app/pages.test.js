import assert from 'node:assert/strict';
import test, { before, after } from 'node:test';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { createServer } from 'vite';
import { fileURLToPath } from 'node:url';

let server;
const loaded = {};
const sourceRoot = fileURLToPath(new URL('../..', import.meta.url));
before(async () => {
  server = await createServer({ root: sourceRoot, configFile: false, appType: 'custom', server: { middlewareMode: true }, optimizeDeps: { noDiscovery: true } });
  for (const name of ['pages/OverviewPage', 'pages/ManualRun', 'pages/ModuleSettings', 'pages/StrategyWorkspace', 'pages/AccountPage', 'pages/ResearchPage', 'pages/SystemPage', 'components/StrategyRuns', 'components/AutomationCard', 'components/PerformancePanel', 'components/Dialog', 'app/PortalShell']) {
    Object.assign(loaded, await server.ssrLoadModule(`/src/${name}.jsx`));
  }
});
after(async () => { await server?.close(); });

const noop = () => {};
function state() {
  const broker = { id: 'paper', name: '练习账户', enabled: true, mode: 'broker_paper', mode_label: '模拟账户', capabilities: ['inspect', 'prepare', 'observe', 'execute', 'reconcile', 'calendar'] };
  return {
    broker, brokers: [broker], broker_verified: false, account: null, source: 'snapshot',
    strategy: { type: 'alpha', lookback: 20 }, strategy_name: '测试策略', config_version: 'v1',
    strategies: [{ type: 'alpha', name: '测试策略', enabled: true, defaults: { lookback: 20 }, fields: [{ name: 'lookback', label: '观察天数', type: 'integer' }], readme: { path: 'README.md', content: '# 选股说明' } }],
    sources: [{ id: 'snapshot', name: '测试快照', enabled: true, capabilities: ['upload', 'preview'], fields: [] }],
    strategy_manager: { instances: [], accounts: [], running_count: 0, limits: {}, unmanaged: {} },
    automation: { enabled: false, status: 'disabled', config: {}, available: false, blockers: [] },
    monitoring: { settings: { enabled: true, channels: [] }, events: [] },
    actions: {}, jobs: [], limits: { max_gross: 1, max_weight: 0.2 }, inventory: { data: [], broker: [] }, backtest_config: {},
  };
}
const common = value => ({ state: value, route: {}, busy: false, pending: false, act: noop, run: noop, navigate: noop, onNavigate: noop, chooseSource: noop, chooseBroker: noop, upload: noop, selectedFile: { current: null }, submit: noop, authorize: noop, bind: noop, disconnect: noop, access: { enabled: false }, shutdown: noop, refresh: noop });
const render = (name, props) => renderToStaticMarkup(React.createElement(loaded[name], props));

test('all retained feature pages render an unconfigured module set without an account read', () => {
  const props = common(state());
  for (const name of ['ManualRun', 'StrategyWorkspace', 'DataPage', 'BrokerPage', 'PositionsPage', 'ResearchPage', 'PerformancePanel']) {
    assert.ok(render(name, props).length > 40, name);
  }
  assert.match(render('SystemPage', { ...props, view: 'logs' }), /运行与日志/);
  assert.match(render('SystemPage', { ...props, view: 'monitoring' }), /监控与通知/);
  assert.match(render('SystemPage', { ...props, view: 'server' }), /下载诊断/);
});

test('overview has no charts, module form, or trading execution button', () => {
  const html = render('OverviewPage', { ...common(state()), runtimeStatus: { label: '后台运行中' } });
  assert.match(html, /创建策略实例/);
  assert.match(html, /当前没有待处理事项/);
  assert.doesNotMatch(html, /<select|账户表现|一键测试并准备|提交模拟订单|自动建仓与调仓/);
});

test('overview limits current blockers to three and links to the complete list', () => {
  const value = state();
  value.strategy_manager.instances = [1, 2, 3, 4].map(index => ({ id: `run-${index}`, name: `实例${index}`, broker_id: 'paper', pending: true, status: 'paused', message: `当前异常${index}` }));
  const html = render('OverviewPage', { ...common(value), runtimeStatus: { label: '后台运行中' } });
  assert.match(html, /当前异常1/);
  assert.match(html, /当前异常3/);
  assert.doesNotMatch(html, /当前异常4/);
  assert.match(html, /查看全部/);
});

test('instance list reveals no operational actions or form until a specific detail is opened', () => {
  const value = state();
  const item = { id: 'one', name: '月度策略', strategy: value.strategy, broker_id: 'paper', source_id: 'snapshot', budget: 0.5, status: 'waiting', enabled: true, schedule: {}, positions: [], events: [], message: '等待计划时间' };
  value.strategy_manager.instances = [item];
  const list = render('StrategyRuns', { ...common(value), route: { page: 'runs', view: 'instances' } });
  assert.match(list, /查看详情/);
  assert.doesNotMatch(list, /停止实例|执行一轮|创建策略实例|运行计划.*<select/);
  const detail = render('StrategyRuns', { ...common(value), busy: true, pending: false, route: { page: 'runs', view: 'instances', instance: 'one' } });
  assert.match(detail, /停止实例/);
  assert.doesNotMatch(detail, /<button[^>]*disabled[^>]*><span>停止实例/);
  assert.match(detail, /运行日志/);
});

test('a saved plan is shown across accounts but reconciliation cannot target the selected account', () => {
  const value = state();
  value.managed_account = true;
  value.actions.reconcile = { enabled: true };
  value.automation = { enabled: true, status: 'waiting', config: {}, binding: { broker_id: 'other', broker_name: '另一账户', strategy_name: '另一个策略' } };
  const html = render('AutomationCard', { ...common(value), online: true, runtimeRunning: true });
  assert.match(html, /另一账户/);
  assert.match(html, /关闭自动运行/);
  assert.match(html, /<button[^>]*disabled[^>]*>核对订单/);
  assert.match(html, /title="先选择计划绑定账户，再核对订单。"/);
});

test('server deployment navigation does not depend on whether the preview requires login', () => {
  const html = render('PortalShell', { route: { page: 'system', view: 'server' }, navigate: noop,
    access: { enabled: false, deployment: 'server' }, session: { state: null, runtime: null, online: true }, runtimeStatus: {}, toggleRuntime: noop });
  assert.match(html, /服务器/);
  assert.match(html, /云端工作台/);
});

test('confirmation lifecycle uses native modal isolation and restores scrolling and trigger focus', () => {
  let opened = 0, closed = 0, focused = 0;
  const trigger = { isConnected: true, focus: options => { assert.deepEqual(options, { preventScroll: true }); ++focused; } };
  const document = { activeElement: trigger, body: { style: { overflow: 'clip' } } };
  const dialog = { ownerDocument: document, open: false, showModal() { ++opened; this.open = true; }, close() { ++closed; this.open = false; } };
  const cleanup = loaded.activateDialog(dialog);
  assert.equal(opened, 1);
  assert.equal(dialog.open, true);
  assert.equal(document.body.style.overflow, 'hidden');
  cleanup();
  assert.equal(closed, 1);
  assert.equal(document.body.style.overflow, 'clip');
  assert.equal(focused, 1);
});

test('confirmation Escape cancellation respects the callers pending permission', () => {
  let prevented = 0, dismissed = 0;
  const event = { preventDefault() { ++prevented; } };
  loaded.dismissDialog(event, false, () => ++dismissed);
  assert.equal(prevented, 1);
  assert.equal(dismissed, 0);
  loaded.dismissDialog(event, true, () => ++dismissed);
  assert.equal(prevented, 2);
  assert.equal(dismissed, 1);
});

test('dialog does not restore focus to a trigger removed by the confirmed action', () => {
  const document = { activeElement: { isConnected: false, focus: () => assert.fail('removed trigger must not be focused') }, body: { style: { overflow: '' } } };
  const dialog = { ownerDocument: document, open: false, showModal() { this.open = true; }, close() { this.open = false; } };
  loaded.activateDialog(dialog)();
  assert.equal(document.body.style.overflow, '');
  const html = render('Dialog', { labelledBy: 'confirm-heading', onDismiss: noop, children: React.createElement('h2', { id: 'confirm-heading' }, '确认操作') });
  assert.match(html, /^<dialog[^>]*aria-modal="true"[^>]*aria-labelledby="confirm-heading"/);
});

test('platform shell does not present one account environment as the whole product', () => {
  const html = render('PortalShell', { route: { page: 'strategies' }, navigate: noop,
    access: { enabled: false }, session: { state: state(), runtime: null, online: true }, runtimeStatus: {}, toggleRuntime: noop });
  assert.doesNotMatch(html, /PAPER|仅模拟账户/);
  assert.match(html, /Strategy Workspace/);
});

test('strategy workspace shows only related deployments and preserves their account environment', () => {
  const value = state();
  value.strategy_manager.instances = [
    { id: 'a', name: 'Alpha deployment', strategy: { type: 'alpha' }, broker_id: 'paper', budget: .4, status: 'stopped' },
    { id: 'b', name: 'Other deployment', strategy: { type: 'beta' }, broker_id: 'paper', budget: .4, status: 'stopped' },
  ];
  const html = render('StrategyWorkspace', { ...common(value), route: { page: 'strategies', strategy: 'alpha', tab: 'runs' } });
  assert.match(html, /Alpha deployment/);
  assert.match(html, /模拟账户/);
  assert.doesNotMatch(html, /Other deployment/);
  assert.match(html, /创建运行实例/);
});

test('deleted strategy links cannot quietly open the current global strategy', () => {
  const html = render('StrategyWorkspace', { ...common(state()), route: { page: 'strategies', strategy: 'deleted', tab: 'research' } });
  assert.match(html, /这个策略已不存在/);
  assert.doesNotMatch(html, /创建回测实验/);
  const create = render('StrategyRuns', { ...common(state()), route: { page: 'runs', view: 'instances', create: true, strategy: 'deleted' } });
  assert.match(create, /所选策略已不可用/);
  assert.doesNotMatch(create, /保存实例/);
});
