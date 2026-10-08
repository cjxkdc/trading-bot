export const navigation = [
  { id: 'overview', label: '总览', caption: 'Dashboard', icon: 'grid' },
  { id: 'strategies', label: '策略', caption: 'Strategy Workspace', icon: 'layers' },
  { id: 'runs', label: '运行', caption: 'Deployments', icon: 'clock' },
  { id: 'account', label: '账户', caption: 'Portfolio', icon: 'briefcase' },
  { id: 'connections', label: '连接', caption: 'Connections', icon: 'link', secondary: true },
  { id: 'system', label: '系统', caption: 'System', icon: 'server', secondary: true },
];

export const sections = {
  runs: [['instances', '策略实例'], ['manual', '手动调仓'], ['plan', '单策略计划']],
  account: [['performance', '账户总览'], ['positions', '持仓与订单']],
  connections: [['broker', '券商账户'], ['data', '数据源']],
  system: [['logs', '运行与日志'], ['monitoring', '监控与通知'], ['server', '服务器']],
};

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
  research: { page: 'strategies', tab: 'research' },
  backtests: { page: 'strategies', tab: 'research' },
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
  return aliases[input.page] ? { ...input, ...aliases[input.page] } : input;
}

export function resolveDestination(destination, server = true) {
  const input = canonical(typeof destination === 'string' ? { page: destination } : destination || {});
  const page = navigation.some(item => item.id === input.page) ? input.page : 'overview';
  const views = sections[page]?.filter(([id]) => server || id !== 'server') || [];
  const view = views.some(([id]) => id === input.view) ? input.view : views[0]?.[0];
  const strategyContext = page === 'strategies' || (page === 'runs' && view === 'instances' && input.create);
  return { page, ...(view ? { view } : {}),
    ...(page === 'runs' && view === 'instances' && input.instance ? { instance: input.instance } : {}),
    ...(page === 'runs' && view === 'instances' && input.create ? { create: true } : {}),
    ...(strategyContext && /^[a-zA-Z0-9_-]+$/.test(input.strategy || '') ? { strategy: input.strategy } : {}),
    ...(page === 'strategies' && ['code', 'readme', 'parameters', 'research', 'runs'].includes(input.tab) ? { tab: input.tab } : {}) };
}

export function routeFromHash(hash, server = true) {
  const [location, query = ''] = String(hash || '').replace(/^#\/?/, '').split('?');
  const [page, view] = location.split('/');
  const params = new URLSearchParams(query);
  return resolveDestination({ page, view, instance: params.get('instance'), create: params.get('create') === '1', strategy: params.get('strategy'), tab: params.get('tab') }, server);
}

export function routeHash(route) {
  const query = new URLSearchParams();
  if (route.instance) query.set('instance', route.instance);
  if (route.create) query.set('create', '1');
  if (route.strategy) query.set('strategy', route.strategy);
  if (route.tab) query.set('tab', route.tab);
  return `#/${route.page}${route.view ? `/${route.view}` : ''}${query.size ? `?${query}` : ''}`;
}
