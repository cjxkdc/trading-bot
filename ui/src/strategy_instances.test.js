import test from 'node:test';
import assert from 'node:assert/strict';
import { instanceDraft, instancePayload, instanceStatus, budgetSummary, latestInstancePreview } from './strategy_instances.js';

test('instance capital percentages convert once and reject invalid budgets', () => {
  const draft = { budget: '50', strategy: { type: 'custom', symbols: '["AAA"]' }, schedule: {} };
  const value = instancePayload(draft, [{ name: 'symbols', type: 'json' }]);
  assert.equal(value.budget, '0.5'); assert.deepEqual(value.strategy.symbols, ['AAA']);
  for (const budget of [0, -1, 101, 'bad']) assert.throws(() => instancePayload({ ...draft, budget }));
  assert.equal(draft.strategy.symbols, '["AAA"]');
});
test('stopped ownership still reserves capital; empty stopped templates do not', () => {
  assert.equal(budgetSummary([{ broker_id: 'paper', budget: '.4', enabled: true },
    { broker_id: 'paper', budget: '.3', positions: [{ ticker: 'A' }] },
    { broker_id: 'paper', budget: '.8', positions: [] }], 'paper'), .7);
});
test('pending orders stay visibly unresolved even after a strategy is stopped', () => {
  assert.equal(instanceStatus({ status: 'stopped', pending: true }).tone, 'amber');
  assert.equal(instanceStatus({ status: 'stopped', pending: true }).label, '待核对订单');
  assert.equal(instanceStatus({ status: 'waiting', enabled: true }).label, '按计划等待');
});

test('selection previews require this instance and the current configuration revision', () => {
  const item = { id: 'current', revision: 'new' };
  const result = { status: 'complete', instance_revision: 'new', research_only: true, execution_ready: false };
  const job = { action: 'strategy-preview', instance_id: 'current', finished_at: 'now', result };
  assert.equal(latestInstancePreview([job], item), result);
  assert.equal(latestInstancePreview([{ ...job, instance_id: 'other' }], item), null);
  assert.equal(latestInstancePreview([job], { ...item, revision: 'changed' }), null);
  assert.equal(latestInstancePreview([{ ...job, finished_at: null }], item), null);
});

test('a failed latest preview and execution results cannot appear as a valid preview', () => {
  const item = { id: 'current', revision: 'new' };
  const result = { status: 'complete', instance_revision: 'new', research_only: true, execution_ready: false };
  const job = { action: 'strategy-preview', instance_id: 'current', finished_at: 'now', result };
  assert.equal(latestInstancePreview([{ ...job, result: { status: 'blocked' } }, job], item), null);
  assert.equal(latestInstancePreview([{ ...job, result: { ...result, execution_ready: true } }], item), null);
  assert.equal(latestInstancePreview([{ ...job, action: 'strategy-cycle' }], item), null);
});
test('a draft chooses registered modules without assuming a vendor', () => {
  const state = { strategy_name: 'Custom', strategy: { type: 'custom' }, broker: { id: 'local', mode: 'paper' },
    brokers: [{ id: 'new_broker', enabled: true, mode: 'broker_paper', capabilities: ['prepare','execute','reconcile','calendar'] }], source: 'file',
    sources: [{ id: 'new_data', enabled: true, capabilities: ['collect'] }] };
  const value = instanceDraft(state);
  assert.equal(value.broker_id, 'new_broker'); assert.equal(value.source_id, 'new_data');
});
