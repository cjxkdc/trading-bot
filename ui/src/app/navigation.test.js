import assert from 'node:assert/strict';
import test from 'node:test';
import { resolveDestination, routeFromHash, routeHash } from './navigation.js';

test('existing backend destinations retain precise functional targets', () => {
  assert.deepEqual(resolveDestination('data'), { page: 'modules', view: 'data' });
  assert.deepEqual(resolveDestination('broker'), { page: 'modules', view: 'broker' });
  assert.deepEqual(resolveDestination('strategy'), { page: 'modules', view: 'strategies' });
  assert.deepEqual(resolveDestination('strategies'), { page: 'modules', view: 'strategies' });
  assert.deepEqual(resolveDestination('positions'), { page: 'account', view: 'positions' });
  assert.deepEqual(resolveDestination('logs'), { page: 'system', view: 'logs' });
  assert.deepEqual(resolveDestination('automation'), { page: 'runs', view: 'plan' });
  assert.deepEqual(resolveDestination('backtests'), { page: 'research' });
});

test('desktop cannot deep-link a server-only view and unknown routes are safe', () => {
  assert.deepEqual(resolveDestination('server', false), { page: 'system', view: 'logs' });
  assert.deepEqual(routeFromHash('#/system/server', false), { page: 'system', view: 'logs' });
  assert.deepEqual(routeFromHash('#/something/unknown'), { page: 'overview' });
  assert.deepEqual(resolveDestination({ page: 'account', view: 'admin' }), { page: 'account', view: 'positions' });
});

test('instance deep links survive reload and browser navigation', () => {
  const route = { page: 'runs', view: 'instances', instance: 'run a/2' };
  assert.deepEqual(routeFromHash(routeHash(route)), route);
  assert.deepEqual(routeFromHash('#/runs/instances?create=1'), { page: 'runs', view: 'instances', create: true });
  assert.deepEqual(routeFromHash('#/data'), { page: 'modules', view: 'data' });
});
