import assert from 'node:assert/strict';
import test from 'node:test';
import { automationControls, automationSchedule, automationView, scheduleChanged, scheduleSummary } from './automation.js';
import { runtimeView } from './runtime.js';

const ready = { enabled: false, status: 'disabled', available: true, blockers: [] };

test('a live service is not an enabled automatic trading plan', () => {
  assert.equal(automationView(ready).label, '已关闭');
  assert.match(runtimeView({ status: 'running', alive: true }, true, false, null, ready).hint, /手动/);
  assert.equal(automationControls(ready, { brokerVerified: true }).canEnable, true);
});

test('lost connection and a stopped backend never show automation as running', () => {
  const running = { ...ready, enabled: true, status: 'running' };
  assert.equal(automationView(running, { online: false }).status, 'unknown');
  assert.notEqual(automationView(running, { online: false }).tone, 'blue');
  assert.equal(automationView(running, { runtimeRunning: false }).status, 'stopped');
  assert.equal(automationControls(running, { online: false }).canDisable, false);
  assert.equal(automationView(null).status, 'unknown');
});

test('enable checks prerequisites automatically but requires capability and a saved plan', () => {
  for (const state of [null, { ...ready, available: false }, { ...ready, blockers: ['未验证模拟账户'] }]) {
    assert.equal(automationControls(state, { brokerVerified: true }).canEnable, false);
  }
  for (const options of [{ dirty: true }, { pending: true }, { busy: true }, { online: false }, { runtimeRunning: false }]) {
    assert.equal(automationControls(ready, { brokerVerified: true, ...options }).canEnable, false);
  }
});

test('a latched fault stays visibly paused after automatic execution is disabled', () => {
  assert.equal(automationView({ ...ready, enabled: false, status: 'paused' }).status, 'paused');
  assert.equal(automationView({ ...ready, enabled: false, status: 'paused' }).tone, 'amber');
  assert.equal(automationControls(ready).canEnable, true);
});

test('disable remains available while busy or blocked, while edits remain locked', () => {
  const enabled = { ...ready, enabled: true, status: 'paused', available: false, blockers: ['存在待核对订单'] };
  const controls = automationControls(enabled, { busy: true });
  assert.equal(controls.canDisable, true);
  assert.equal(controls.canEnable, false);
  assert.equal(controls.canEdit, false);
  assert.equal(automationView(enabled).tone, 'amber');
});

test('schedule comparison handles browser number input strings without dropping fields', () => {
  const config = { cadence: 'monthly', day: 3, prepare_time: '05:30', trade_time: '10:05', timezone: 'America/New_York' };
  const draft = { ...automationSchedule(config), day: '3' };
  assert.equal(scheduleChanged(draft, config), false);
  assert.equal(scheduleChanged({ ...draft, trade_time: '10:10' }, config), true);
  assert.deepEqual(automationSchedule(config), { cadence: 'monthly', day: 3, weekday: 0, prepare_time: '05:30', trade_time: '10:05' });
});

test('initial account check and first portfolio have distinct visible statuses', () => {
  assert.equal(automationView({ ...ready, enabled:true, status:'waiting', startup:{status:'pending_check'} }).label, '首次检查中');
  assert.equal(automationView({ ...ready, enabled:true, status:'waiting', startup:{status:'checked'} }).label, '等待首次建仓');
  assert.equal(automationView({ ...ready, enabled:true, status:'waiting', startup:{status:'complete'} }).label, '等待调仓');
});

test('cadence summaries and weekday changes preserve the actual saved schedule', () => {
  assert.match(scheduleSummary({cadence:'daily'}), /每个交易日/);
  assert.match(scheduleSummary({cadence:'weekly',weekday:2}), /每周三/);
  assert.match(scheduleSummary({cadence:'monthly',day:3}), /每月 3 日/);
  const config = {cadence:'weekly',weekday:2};
  assert.equal(scheduleChanged({...automationSchedule(config),weekday:3}, config),true);
});

test('closing unsubmitted remainders requires both server permissions and automation explicitly off', () => {
  const options = { reconcileReady: true, resolveRemainderReady: true };
  assert.equal(automationControls(ready, options).canResolveRemainder, true);
  for (const state of [null, { ...ready, enabled: true }, { ...ready, enabled: undefined }]) {
    assert.equal(automationControls(state, options).canResolveRemainder, false);
  }
  for (const blocked of [{ reconcileReady: false }, { resolveRemainderReady: false },
    { pending: true }, { busy: true }, { online: false }, { runtimeRunning: false }]) {
    assert.equal(automationControls(ready, { ...options, ...blocked }).canResolveRemainder, false);
  }
  assert.equal(automationControls(ready).canResolveRemainder, false);
});
