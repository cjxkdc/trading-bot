import assert from 'node:assert/strict';
import test from 'node:test';
import { researchSources, resolveResearchSource, reconcileResearchSelection, researchExperiments, researchResultSelection } from './research_context.js';

function state() {
  return {
    strategy: { type: 'alpha' }, automation: { config: { frequency: 'monthly' } },
    strategy_manager: { instances: [
      { id: 'alpha-one', name: 'Alpha one', strategy: { type: 'alpha', period: 20 }, schedule: { frequency: 'daily' } },
      { id: 'beta-one', name: 'Beta one', strategy: { type: 'beta' }, schedule: { frequency: 'monthly' } },
      { id: 'alpha-two', name: 'Alpha two', strategy: { type: 'alpha', period: 60 }, schedule: { frequency: 'weekly' } },
    ] },
  };
}

test('research sources include only matching instances and matching manual configuration', () => {
  const value = state();
  assert.deepEqual(researchSources(value, 'alpha').map(x => x.id), ['instance:alpha-one', 'instance:alpha-two', 'manual']);
  assert.deepEqual(researchSources(value, 'beta').map(x => x.id), ['instance:beta-one']);
  assert.deepEqual(researchSources(value, 'gamma'), []);
  assert.deepEqual(researchSources(value, undefined), []);
  assert.equal(resolveResearchSource(value, 'beta', { strategyType: 'beta', sourceId: 'manual' }), null);
  assert.equal(resolveResearchSource(value, 'alpha', { strategyType: 'alpha', sourceId: 'instance:beta-one' }), null);
});

test('different instances of one strategy keep their own parameters and schedule', () => {
  const value = state();
  const first = resolveResearchSource(value, 'alpha', { strategyType: 'alpha', sourceId: 'instance:alpha-one' });
  const second = resolveResearchSource(value, 'alpha', { strategyType: 'alpha', sourceId: 'instance:alpha-two' });
  assert.equal(first.instance.strategy.period, 20);
  assert.equal(second.instance.strategy.period, 60);
  assert.equal(first.schedule.frequency, 'daily');
  assert.equal(second.schedule.frequency, 'weekly');
});

test('deleted source invalidates selection without falling back to another source', () => {
  const value = state();
  const original = { strategyType: 'alpha', sourceId: 'instance:alpha-one' };
  assert.equal(reconcileResearchSelection(value, 'alpha', original), original);
  value.strategy_manager.instances = value.strategy_manager.instances.filter(x => x.id !== 'alpha-one');
  assert.equal(resolveResearchSource(value, 'alpha', original), null);
  const cleared = reconcileResearchSelection(value, 'alpha', original);
  assert.deepEqual(cleared, { strategyType: 'alpha', sourceId: '' });
  value.strategy_manager.instances.unshift(state().strategy_manager.instances[0]);
  assert.equal(resolveResearchSource(value, 'alpha', cleared), null);
});

test('changing strategy or reassigning a source requires an explicit new selection', () => {
  const value = state();
  const manual = { strategyType: 'alpha', sourceId: 'manual' };
  assert.equal(resolveResearchSource(value, 'beta', manual), null);
  value.strategy.type = 'beta';
  assert.deepEqual(reconcileResearchSelection(value, 'alpha', manual), { strategyType: 'alpha', sourceId: '' });
  const instance = { strategyType: 'alpha', sourceId: 'instance:alpha-one' };
  value.strategy_manager.instances[0].strategy.type = 'beta';
  assert.equal(resolveResearchSource(value, 'alpha', instance), null);
  assert.deepEqual(reconcileResearchSelection(value, 'alpha', instance), { strategyType: 'alpha', sourceId: '' });
});

test('experiments are scoped by frozen strategy type; invalid or unowned records stay separate', () => {
  const items = [
    { id: 'alpha', configuration: { strategy: { type: 'alpha' } }, results: [] },
    { id: 'beta', configuration: { strategy: { type: 'beta' } }, results: [] },
    { id: 'invalid', invalid: true, configuration: { strategy: { type: 'alpha' } } },
    { id: 'missing', configuration: {} },
  ];
  const scoped = researchExperiments(items, 'alpha');
  assert.deepEqual(scoped.matching.map(x => x.id), ['alpha']);
  assert.deepEqual(scoped.unassigned.map(x => x.id), ['invalid', 'missing']);
  assert.deepEqual(researchExperiments(items, undefined).matching, []);
});

test('comparison selection excludes other strategies and invalid or removed results', () => {
  const items = [
    { id: 'alpha', configuration: { strategy: { type: 'alpha' } }, results: [{ id: 'a/1' }, { id: 'a/2', invalid: true }] },
    { id: 'beta', configuration: { strategy: { type: 'beta' } }, results: [{ id: 'b/1' }] },
  ];
  const scoped = researchExperiments(items, 'alpha').matching;
  assert.deepEqual(researchResultSelection(scoped, ['a/1', 'a/1', 'a/2', 'b/1', 'deleted']), ['a/1']);
});
