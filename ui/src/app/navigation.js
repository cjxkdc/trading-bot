// One section per job. Each entry is a place the user goes to do one thing;
// the intro line under the page title says what that thing is.
export const navigation = [
  { id: 'overview', label: '现在', caption: 'Now', icon: 'home', group: '工作' },
  { id: 'strategies', label: '策略', caption: 'Strategies', icon: 'code', group: '工作' },
  { id: 'backtests', label: '回测', caption: 'Backtests', icon: 'chart', group: '工作' },
  { id: 'runs', label: '运行', caption: 'Runs', icon: 'play', group: '工作' },
  { id: 'account', label: '账户', caption: 'Portfolio', icon: 'briefcase', group: '资产' },
  { id: 'connections', label: '连接', caption: 'Connections', icon: 'plug', group: '设置', secondary: true },
  { id: 'system', label: '系统', caption: 'System', icon: 'settings', group: '设置', secondary: true },
];

export const intros = {
  overview: '后台在不在，今天跑什么，有没有要你处理的事。',
  strategies: '写和改策略的代码与说明；账户、资金和计划属于「运行」。',
  backtests: '选一个策略和区间，导出到 QuantConnect 跑回测，再把结果导回来比较。',
  runs: '让一个策略在一个账户上跑起来，并看它跑得怎么样。',
  account: '看持仓、订单、净值和资金流水；这里只看，不改。',
  connections: '配置并检查券商账户和数据源。',
  system: '后台开关、操作记录、监控通知和服务器。',
};

export const sections = {
  runs: [['instances', '运行列表'], ['manual', '手动调仓'], ['plan', '单策略计划']],
  account: [['performance', '账户总览'], ['positions', '持仓与订单']],
  connections: [['broker', '券商账户'], ['data', '数据源']],
  system: [['logs', '运行与日志'], ['monitoring', '监控与通知'], ['server', '服务器']],
};

// Views that stay reachable (links, old bookmarks) but are not offered as tabs:
// they are demoted ways of doing what the section's main view does.
export const quietViews = { runs: ['manual', 'plan'] };

export function visibleTabs(page, server = true) {
  return (sections[page] || []).filter(([id]) => (server || id !== 'server') && !(quietViews[page] || []).includes(id));
}

const aliases = {
  home: { page: 'overview' },
  instances: { page: 'runs', view: 'instances' },
  manual: { page: 'runs', view: 'manual' },
  automation: { page: 'runs', view: 'plan' },
  strategy: { page: 'strategies' },
  data: { page: 'connections', view: 'data' },
  broker: { page: 'connections', view: 'broker' },
  positions: { page: 'account', view: 'positions' },
  performance: { page: 'account', view: 'performance' },
  research: { page: 'backtests' },
  backtests: { page: 'backtests' },
  logs: { page: 'system', view: 'logs' },
  server: { page: 'system', view: 'server' },
};

function canonical(input) {
  // Keep bookmarks, notification destinations and editor links from older releases.
  if (input.page === 'modules') {
    return ['data', 'broker'].includes(input.view)
      ? { ...input, page: 'connections' }
      : { ...input, page: 'strategies', view: undefined };
  }
  if (input.page === 'strategies' && input.tab === 'research') return { ...input, page: 'backtests', tab: undefined };
  return aliases[input.page] ? { ...input, ...aliases[input.page] } : input;
}

export function resolveDestination(destination, server = true) {
  const input = canonical(typeof destination === 'string' ? { page: destination } : destination || {});
  const page = navigation.some(item => item.id === input.page) ? input.page : 'overview';
  const views = sections[page]?.filter(([id]) => server || id !== 'server') || [];
  const view = views.some(([id]) => id === input.view) ? input.view : views[0]?.[0];
  const strategyContext = page === 'strategies' || page === 'backtests' || (page === 'runs' && view === 'instances' && input.create);
  return { page, ...(view ? { view } : {}),
    ...(page === 'runs' && view === 'instances' && input.instance ? { instance: input.instance } : {}),
    ...(page === 'runs' && view === 'instances' && input.create ? { create: true } : {}),
    ...(strategyContext && /^[a-zA-Z0-9_-]+$/.test(input.strategy || '') ? { strategy: input.strategy } : {}),
    ...(page === 'strategies' && ['code', 'readme', 'parameters', 'runs'].includes(input.tab) ? { tab: input.tab } : {}),
    // Viewing another account's records never changes the operating account.
    ...(page === 'account' && /^[a-zA-Z0-9_-]+$/.test(input.broker || '') ? { broker: input.broker } : {}),
    // An error banner's 诊断编号 opens the log search with that id filled in.
    ...(page === 'system' && view === 'logs' && /^[0-9a-f]{6,40}$/.test(input.incident || '') ? { incident: input.incident } : {}) };
}

export function routeFromHash(hash, server = true) {
  const [location, query = ''] = String(hash || '').replace(/^#\/?/, '').split('?');
  const [page, view] = location.split('/');
  const params = new URLSearchParams(query);
  return resolveDestination({ page, view, instance: params.get('instance'), create: params.get('create') === '1', strategy: params.get('strategy'), tab: params.get('tab'), broker: params.get('broker'), incident: params.get('incident') }, server);
}

export function routeHash(route) {
  const query = new URLSearchParams();
  if (route.instance) query.set('instance', route.instance);
  if (route.create) query.set('create', '1');
  if (route.strategy) query.set('strategy', route.strategy);
  if (route.tab) query.set('tab', route.tab);
  if (route.broker) query.set('broker', route.broker);
  if (route.incident) query.set('incident', route.incident);
  const search = query.toString();
  return `#/${route.page}${route.view ? `/${route.view}` : ''}${search ? `?${search}` : ''}`;
}
