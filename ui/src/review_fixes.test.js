import { test } from 'node:test';
import assert from 'node:assert/strict';
import { budgetFraction, budgetPercent, instancePayload, withTimeDefaults } from './strategy_instances.js';
import { resolveDestination, routeFromHash, routeHash } from './app/navigation.js';
import { runtimeView } from './runtime.js';
import { automationControls, automationOrderCheck } from './automation.js';
import { backfillPercent, backfillSummary, defaultBackfillRange, durationLabel } from './backfill.js';

test('backfill helpers describe the queue and its cost in plain words', () => {
  assert.deepEqual(defaultBackfillRange(new Date(2026, 9, 11)), { start: '2025-10-01', end: '2026-10-11', cadence: 'monthly' });
  assert.equal(durationLabel(45), '45 分钟');
  assert.equal(durationLabel(150), '2 小时 30 分钟');
  assert.equal(durationLabel(120), '2 小时');
  const queue = { status: 'paused', start: '2026-01-01', end: '2026-04-01', cadence: 'monthly', total: 4, finished_count: 2, counts: { done: 1, failed: 1, pending: 2 } };
  assert.equal(backfillSummary(queue), '2026-01-01 → 2026-04-01（每月）：已暂停，完成 1 个日期，失败 1。');
  assert.equal(backfillPercent(queue), 50);
  assert.equal(backfillSummary(null), null);
  assert.equal(backfillPercent({ total: 0 }), 0);
});

test('instance budgets round-trip without binary float noise', () => {
  assert.equal(budgetPercent('0.07'), 7);
  assert.equal(budgetPercent('0.29'), 29);
  assert.equal(budgetPercent('0.1234'), 12.34);
  assert.equal(budgetFraction(7), '0.07');
  assert.equal(budgetFraction(29), '0.29');
  assert.equal(budgetFraction(100), '1');
  const draft = { name: 'x', strategy: { type: 'mom8' }, broker_id: 'b', source_id: 's', budget: 7, schedule: {} };
  assert.equal(instancePayload(draft).budget, '0.07');
});

test('a new run never opens with empty schedule times, and saved times win over defaults', () => {
  const broker = { execution_context: { timezone: 'Asia/Hong_Kong', schedule_defaults: { trade_time: '09:40' } } };
  assert.deepEqual(withTimeDefaults({ cadence: 'monthly', timezone: '', prepare_time: '', trade_time: '' }, broker),
    { cadence: 'monthly', timezone: 'Asia/Hong_Kong', prepare_time: '08:00', trade_time: '09:40' });
  assert.deepEqual(withTimeDefaults({ timezone: 'America/New_York', prepare_time: '06:00', trade_time: '09:35' }, null),
    { timezone: 'America/New_York', prepare_time: '06:00', trade_time: '09:35' });
  assert.equal(withTimeDefaults({ prepare_time: '', trade_time: '' }, { execution_context: {} }).trade_time, '09:35');
});

test('the account page keeps a viewed broker in the route without touching other pages', () => {
  const route = routeFromHash('#/account/positions?broker=moomoo_paper');
  assert.deepEqual(route, { page: 'account', view: 'positions', broker: 'moomoo_paper' });
  assert.equal(routeHash(route), '#/account/positions?broker=moomoo_paper');
  // The section tabs pass the current broker along, so switching tabs keeps the viewed account.
  assert.equal(routeHash(resolveDestination({ page: 'account', view: 'performance', broker: route.broker })),
    '#/account/performance?broker=moomoo_paper');
  assert.equal(resolveDestination({ page: 'runs', view: 'manual', broker: 'moomoo_paper' }).broker, undefined);
  assert.equal(resolveDestination({ page: 'account', broker: '../evil' }).broker, undefined);
  assert.equal(routeHash({ page: 'overview' }), '#/overview');
});

test('an incident id travels to the log search and nowhere else', () => {
  const route = routeFromHash('#/system/logs?incident=3f9a1c2b7d');
  assert.deepEqual(route, { page: 'system', view: 'logs', incident: '3f9a1c2b7d' });
  assert.equal(routeHash(route), '#/system/logs?incident=3f9a1c2b7d');
  assert.equal(resolveDestination({ page: 'system', view: 'monitoring', incident: '3f9a1c2b7d' }).incident, undefined);
  assert.equal(resolveDestination({ page: 'system', view: 'logs', incident: '../x' }).incident, undefined);
});

test('a live but unresponsive backend can be asked to stop, never started twice', () => {
  const view = runtimeView({ status: 'unresponsive', alive: true, message: '后台没有响应' }, true, false, null, null);
  assert.equal(view.disabled, false);
  assert.equal(view.checked, true);
  assert.match(view.hint, /没有响应/);
  const dead = runtimeView({ status: 'failed', alive: false }, true, false, null, null);
  assert.equal(dead.disabled, false); // start is allowed again
  const busy = runtimeView({ status: 'running', alive: true }, true, false, { action: 'execute' }, null);
  assert.equal(busy.disabled, true);
});

test('the untransmitted closure follows the same gates as the other ledger closures', () => {
  const automation = { enabled: false, status: 'disabled', available: true, blockers: [] };
  const controls = automationControls(automation, { reconcileReady: true, closeUntransmittedReady: true });
  assert.equal(controls.canCloseUntransmitted, true);
  assert.equal(automationControls(automation, { reconcileReady: false, closeUntransmittedReady: true }).canCloseUntransmitted, false);
  const jobs = [{ broker: 'b', action: 'reconcile', status: 'paused', finished_at: '2026-10-09T01:00:00+00:00',
    result: { recovery_state: 'blocked', details: { terminal: 0, untransmitted: 2, orders: [] } } }];
  assert.equal(automationOrderCheck(null, jobs, 'b').untransmitted, 2);
});
