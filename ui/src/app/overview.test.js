import assert from 'node:assert/strict';
import test from 'node:test';
import { attentionItems, nextRun, runningRows, visiblePlan } from './overview.js';

test('an active plan bound to another account remains visible with instance ownership', () => {
  const state = { managed_account: true, broker: { id: 'new', name: '当前实例账户' }, brokers: [],
    strategy_manager: { instances: [] }, automation: { enabled: true, status: 'waiting', binding: { strategy_name: '旧策略', broker_name: '独立计划账户' } } };
  const [row] = runningRows(state);
  assert.equal(row.account, '独立计划账户');
  assert.match(row.name, /旧策略/);
  assert.equal(row.destination, 'automation');
  assert.equal(row.next, undefined);
  assert.equal(visiblePlan({ enabled: false, status: 'paused' }), true);
  assert.equal(visiblePlan({ enabled: false, status: 'disabled' }), false);
});

test('historic failures and ordinary unconfigured modules do not become current alerts', () => {
  const state = { strategy_manager: { instances: [] }, automation: { enabled: false, status: 'disabled' },
    jobs: [{ status: 'blocked', message: '历史故障' }], sources: [{ configured: false }],
    monitoring: { events: [{ severity: 'error', message: '已恢复历史事件' }] } };
  assert.deepEqual(attentionItems(state), []);
});

test('current blockers are actionable and distinguish an instance from the old plan', () => {
  const state = { strategy_manager: { corrupt: true, instances: [{ id: 'i1', name: '实例一', pending: true, message: '等待成交核对' }] },
    automation: { status: 'paused', message: '模块需重新核验' }, account: { status: 'blocked', message: '连接未通过' }, broker: { name: '测试账户' } };
  const items = attentionItems(state);
  assert.equal(items.length, 4);
  assert.deepEqual(items[1].destination, { page: 'runs', view: 'instances', instance: 'i1' });
  assert.equal(items[2].destination, 'automation');
  assert.equal(items[3].destination, 'broker');
});

test('next execution excludes stopped records and unknown or malformed timestamps', () => {
  assert.equal(nextRun([{ enabled: false, next: '2026-01-01T00:00:00Z' }, { enabled: true, next: 'unknown' }]), null);
  assert.equal(nextRun([{ enabled: true, next: '2026-10-08T12:00:00Z' }, { enabled: true, next: '2026-10-07T12:00:00Z' }]), '2026-10-07T12:00:00Z');
});
