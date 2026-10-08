export const navigation = [
  { id: 'overview', label: '总览', caption: 'Dashboard', icon: 'grid' },
  { id: 'runs', label: '策略运行', caption: 'Strategy Runs', icon: 'clock' },
  { id: 'account', label: '账户', caption: 'Portfolio', icon: 'briefcase' },
  { id: 'modules', label: '模块配置', caption: 'Algorithm Lab', icon: 'layers' },
  { id: 'research', label: '研究', caption: 'Backtests', icon: 'chart', secondary: true },
  { id: 'system', label: '系统', caption: 'System', icon: 'server', secondary: true },
];

export const sections = {
  runs: [['instances', '策略实例'], ['manual', '手动调仓'], ['plan', '单策略计划']],
  account: [['performance', '账户总览'], ['positions', '持仓与订单']],
  modules: [['strategies', '策略'], ['data', '数据'], ['broker', '券商']],
  system: [['logs', '运行与日志'], ['monitoring', '监控与通知'], ['server', '服务器']],
};

const aliases = {
  home: { page: 'overview' },
  instances: { page: 'runs', view: 'instances' },
  manual: { page: 'runs', view: 'manual' },
  automation: { page: 'runs', view: 'plan' },
  strategy: { page: 'modules', view: 'strategies' },
  strategies: { page: 'modules', view: 'strategies' },
  data: { page: 'modules', view: 'data' },
  broker: { page: 'modules', view: 'broker' },
  positions: { page: 'account', view: 'positions' },
  performance: { page: 'account', view: 'performance' },
  backtests: { page: 'research' },
  logs: { page: 'system', view: 'logs' },
  server: { page: 'system', view: 'server' },
};

export function resolveDestination(destination, server = true) {
  const input = typeof destination === 'string' ? aliases[destination] || { page: destination } : destination || {};
  const page = navigation.some(item => item.id === input.page) ? input.page : 'overview';
  const views = sections[page]?.filter(([id]) => server || id !== 'server') || [];
  const view = views.some(([id]) => id === input.view) ? input.view : views[0]?.[0];
  const strategy = page === 'modules' && view === 'strategies';
  return { page, ...(view ? { view } : {}), ...(page === 'runs' && view === 'instances' && input.instance ? { instance: input.instance } : {}), ...(page === 'runs' && view === 'instances' && input.create ? { create: true } : {}), ...(strategy && /^[a-zA-Z0-9_-]+$/.test(input.strategy || '') ? { strategy: input.strategy } : {}), ...(strategy && ['code', 'readme', 'parameters'].includes(input.tab) ? { tab: input.tab } : {}) };
}

export function routeFromHash(hash, server = true) {
  const [location, query = ''] = String(hash || '').replace(/^#\/?/, '').split('?');
  const [page, view] = location.split('/');
  const params = new URLSearchParams(query);
  return resolveDestination({ ...(view ? { page, view } : aliases[page] || { page }), instance: params.get('instance'), create: params.get('create') === '1', strategy: params.get('strategy'), tab: params.get('tab') }, server);
}

export function routeHash(route) {
  const query = new URLSearchParams();
  if (route.instance) query.set('instance', route.instance);
  if (route.create) query.set('create', '1');
  if (route.strategy) query.set('strategy', route.strategy);
  if (route.tab) query.set('tab', route.tab);
  return `#/${route.page}${route.view ? `/${route.view}` : ''}${query.size ? `?${query}` : ''}`;
}
