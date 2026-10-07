export const navigation = [
  { id: 'overview', label: '总览', icon: 'grid' },
  { id: 'runs', label: '策略运行', icon: 'clock' },
  { id: 'account', label: '账户', icon: 'briefcase' },
  { id: 'modules', label: '模块配置', icon: 'layers' },
  { id: 'research', label: '研究', icon: 'chart', secondary: true },
  { id: 'system', label: '系统', icon: 'server', secondary: true },
];

export const sections = {
  runs: [['instances', '策略实例'], ['manual', '手动调仓'], ['plan', '单策略计划']],
  account: [['positions', '持仓与订单'], ['performance', '账户表现']],
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
  return { page, ...(view ? { view } : {}), ...(page === 'runs' && view === 'instances' && input.instance ? { instance: input.instance } : {}), ...(page === 'runs' && view === 'instances' && input.create ? { create: true } : {}) };
}

export function routeFromHash(hash, server = true) {
  const [location, query = ''] = String(hash || '').replace(/^#\/?/, '').split('?');
  const [page, view] = location.split('/');
  const params = new URLSearchParams(query);
  return resolveDestination(view ? { page, view, instance: params.get('instance'), create: params.get('create') === '1' } : aliases[page] || { page }, server);
}

export function routeHash(route) {
  const query = new URLSearchParams();
  if (route.instance) query.set('instance', route.instance);
  if (route.create) query.set('create', '1');
  return `#/${route.page}${route.view ? `/${route.view}` : ''}${query.size ? `?${query}` : ''}`;
}
