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
  assert.deepEqual(resolveDestination({ page: 'account', view: 'admin' }), { page: 'account', view: 'performance' });
});

test('strategy workspace preserves selected module and editor tab through browser navigation', () => {
  const route = { page: 'modules', view: 'strategies', strategy: 'mom8', tab: 'code' };
  assert.deepEqual(routeFromHash(routeHash(route)), route);
  assert.deepEqual(routeFromHash('#/strategies?strategy=mom8&tab=code'), route);
  assert.deepEqual(routeFromHash('#/modules/strategies?strategy=../private&tab=unknown'), { page: 'modules', view: 'strategies' });
});

test('instance deep links survive reload and browser navigation', () => {
  const route = { page: 'runs', view: 'instances', instance: 'run a/2' };
  assert.deepEqual(routeFromHash(routeHash(route)), route);
  assert.deepEqual(routeFromHash('#/runs/instances?create=1'), { page: 'runs', view: 'instances', create: true });
  assert.deepEqual(routeFromHash('#/data'), { page: 'modules', view: 'data' });
});

test('a stale lazy chunk reloads the page once, then surfaces the error instead of looping', async () => {
  const { reloadOnceForStaleBuild } = await import('./stale_build.js');
  const store = new Map(), storage = { getItem: key => store.get(key) ?? null, setItem: (key, value) => store.set(key, value) };
  let reloads = 0;
  const location = { reload: () => ++reloads };
  const error = new TypeError('Failed to fetch dynamically imported module');
  const pending = reloadOnceForStaleBuild(error, storage, location);
  assert.ok(pending instanceof Promise);
  assert.equal(reloads, 1);
  assert.throws(() => reloadOnceForStaleBuild(error, storage, location), TypeError);
  assert.equal(reloads, 1);
  const blocked = { getItem() { throw new Error('blocked'); }, setItem() { throw new Error('blocked'); } };
  reloadOnceForStaleBuild(error, blocked, location);
  assert.equal(reloads, 2);
});
