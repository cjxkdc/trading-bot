import test from 'node:test';
import assert from 'node:assert/strict';
import { instanceDraft, instancePayload, instanceStatus, budgetSummary } from './strategy_instances.js';

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
  assert.equal(instanceStatus({ status: 'waiting', enabled: true }).label, '按计划等待');
});
test('a draft chooses registered modules without assuming a vendor', () => {
  const state = { strategy_name: 'Custom', strategy: { type: 'custom' }, broker: { id: 'local', mode: 'paper' },
    brokers: [{ id: 'new_broker', enabled: true, mode: 'broker_paper', capabilities: ['prepare','execute','reconcile','calendar'] }], source: 'file',
    sources: [{ id: 'new_data', enabled: true, capabilities: ['collect'] }] };
  const value = instanceDraft(state);
  assert.equal(value.broker_id, 'new_broker'); assert.equal(value.source_id, 'new_data');
});
