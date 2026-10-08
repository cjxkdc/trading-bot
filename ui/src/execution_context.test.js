import test from 'node:test';
import assert from 'node:assert/strict';
import { executionContext, validTimezone, timezoneLabel, currencyLabel, environmentLabel, formatZoned, brokerCanExecute } from './execution_context.js';
import { runnableBroker, instanceDraft } from './strategy_instances.js';
import { automationSchedule, scheduleSummary } from './automation.js';
import { visiblePositions } from './account_report.js';

test('market/environment display uses descriptor metadata without US or paper fallback', () => {
  const broker = { mode:'live', mode_label:'实盘', execution_context:{market:'HK', currency:'HKD', timezone:'Asia/Hong_Kong', asset_class:'equity'} };
  assert.equal(executionContext(broker).currency, 'HKD');
  assert.equal(executionContext(broker, {currency:'EUR'}).currency, 'EUR');
  assert.equal(executionContext(broker, {currency:null}).currency, 'HKD');
  assert.equal(currencyLabel(executionContext(null).currency), '币种未提供');
  assert.equal(environmentLabel(broker), '实盘');
  assert.equal(environmentLabel({}), '环境待核验');
  assert.equal(timezoneLabel(undefined), '时区未提供');
  assert.equal(validTimezone('bad/timezone'), false);
  assert.equal(formatZoned('2026-10-08T20:00:00Z', undefined, {day:'2-digit'}), '—');
});

test('live and plugin-defined environments can be configured; execution needs explicit permission', () => {
  const broker = {id:'custom', enabled:true, mode:'live', capabilities:['prepare','execute','reconcile','calendar']};
  assert.equal(runnableBroker(broker), true);
  assert.equal(runnableBroker({...broker, mode:'custom_exchange'}), true);
  assert.equal(runnableBroker({...broker, capabilities:['inspect']}), false);
  assert.equal(brokerCanExecute(broker), false);
  assert.equal(brokerCanExecute({...broker, execution_enabled:true}), true);
  assert.equal(brokerCanExecute({...broker, execution_enabled:true, enabled:false}), false);
});

test('new instances inherit the chosen market timezone and preserve saved instance schedules', () => {
  const broker={id:'hk', enabled:true, mode:'live', capabilities:['prepare','execute','reconcile','calendar'], execution_context:{timezone:'Asia/Hong_Kong',schedule_defaults:{prepare_time:'08:00',trade_time:'09:35'}}};
  const state={strategy:{type:'custom'}, strategy_name:'Custom', broker, brokers:[broker], sources:[], automation:{config:{timezone:'America/New_York',prepare_time:'06:00',trade_time:'09:35'}}};
  const draft=instanceDraft(state);
  assert.equal(draft.schedule.timezone, 'Asia/Hong_Kong');
  assert.equal(draft.schedule.prepare_time, '08:00');
  assert.equal(instanceDraft(state, {...draft,budget:0.5,schedule:{timezone:'UTC'}}).schedule.timezone, 'UTC');
  assert.equal(automationSchedule().timezone, '');
  assert.match(scheduleSummary({cadence:'weekly',weekday:6,timezone:'UTC',trade_time:'00:00'}), /周日.*UTC/);
});

test('short positions are visible and zero holdings remain excluded', () => {
  const rows=[{symbol:'BTC',quantity:-0.2,market_value:-100},{symbol:'ETH',quantity:0}];
  assert.deepEqual(visiblePositions(rows,'','market_value').map(p=>p.symbol), ['BTC']);
});
