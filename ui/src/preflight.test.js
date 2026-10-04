import assert from 'node:assert/strict';
import test from 'node:test';
import { preflightControls, preflightProgress } from './preflight.js';

test('missing account, missing snapshots and closed markets can all start one preflight job', () => {
  for (const prerequisites of [{ broker_verified: false, account: null }, { snapshot: null, preview: null },
    { preview: { execution_ready: false }, market_open: false }, { preview: { expired: true, execution_ready: false } }]) {
    const controls = preflightControls({ ...prerequisites, actions: { preflight: { enabled: true }, execute: { enabled: false } } });
    assert.equal(controls.canTest, true);
    assert.deepEqual(controls.request, { action: 'preflight' });
    assert.equal(controls.showSubmit, false);
  }
});

test('the test CTA never turns into execute even when orders are ready', () => {
  const state = { preview: { execution_ready: true }, actions: { preflight: { enabled: true }, execute: { enabled: true } } };
  assert.deepEqual(preflightControls(state).request, { action: 'preflight' });
  assert.equal(preflightControls(state).showSubmit, true);
  assert.equal(preflightControls({ ...state, actions: { execute: { enabled: false } } }).showSubmit, false);
  assert.equal(preflightControls({ ...state, preview: null }).showSubmit, false);
});

test('busy, unavailable and unknown backend state prevent launching duplicate work', () => {
  assert.equal(preflightControls({ actions: { preflight: { enabled: true } } }, true).canTest, false);
  assert.equal(preflightControls({ actions: { preflight: { enabled: false } } }).canTest, false);
  assert.equal(preflightControls(null).canTest, false);
});

test('a completed no-order check is shown as complete without offering submission', () => {
  const controls = preflightControls({ actions: { preflight: { enabled: true }, execute: { enabled: true } },
    preview: { status: 'complete', no_orders: true, execution_ready: false }, preflight_job: { status: 'complete', result: { no_orders: true } } });
  assert.equal(controls.noOrders, true);
  assert.equal(controls.showSubmit, false);
  assert.equal(controls.canTest, true);
});

test('an earlier no-order preflight cannot hide a later manual prepared order', () => {
  const state = { actions: { preflight: { enabled: true }, execute: { enabled: true } },
    preview: { status: 'previewed', execution_ready: true, orders: [{ ticker: 'TEST', quantity: 1 }] },
    preflight_job: { status: 'complete', result: { no_orders: true } } };
  assert.equal(preflightControls(state).noOrders, false);
  assert.equal(preflightControls(state).showSubmit, true);
  assert.equal(preflightControls({ ...state, preview: null }).noOrders, false);
});

test('a closed-market last step preserves earlier passes and the actual reason', () => {
  const result = preflightProgress({ action: 'preflight', status: 'blocked', message: 'not used', steps: [
    { id: 'account', label: '账户', status: 'passed' }, { id: 'data', label: '数据', status: 'passed' },
    { id: 'refresh', label: '采集', status: 'passed' }, { id: 'preview', label: '策略', status: 'passed' },
    { id: 'prepare', label: '订单准备', status: 'blocked', message: '当前美股休市，请在交易时段准备模拟订单。' },
  ] });
  assert.equal(result.passed, 4);
  assert.equal(result.blocked.id, 'prepare');
  assert.match(result.summary, /^4 项通过/);
  assert.equal(result.message, '当前美股休市，请在交易时段准备模拟订单。');
});

test('account failure remains visible and skipped steps never count as passed', () => {
  const result = preflightProgress({ action: 'preflight', status: 'blocked', steps: [
    { id: 'account', label: '账户', status: 'blocked', message: '请登录所选券商的 OpenD。' },
    { id: 'data', label: '数据', status: 'skipped' },
  ] });
  assert.equal(result.passed, 0);
  assert.equal(result.message, '请登录所选券商的 OpenD。');
  assert.equal(preflightProgress(null), null);
  assert.equal(preflightProgress({ action: 'execute', steps: [] }), null);
});

test('passed steps with data limitations retain visible warnings rather than unconditional success', () => {
  const progress = preflightProgress({ action: 'preflight', status: 'previewed', steps: [
    { id: 'account', label: '账户', status: 'passed' },
    { id: 'refresh', label: '数据更新', status: 'passed', warnings: ['部分候选股票缺少历史数据。'] },
    { id: 'preview', label: '策略', status: 'passed', warnings: ['行情存在延迟。', null, ''] },
  ] });
  assert.equal(progress.passed, 3);
  assert.equal(progress.warnings.length, 2);
  assert.match(progress.summary, /2 项有说明/);
  assert.deepEqual(progress.warnings[1].warnings, ['行情存在延迟。']);
});
