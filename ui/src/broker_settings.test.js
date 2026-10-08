import test from 'node:test';
import assert from 'node:assert/strict';
import { brokerChoices, settingsValues, settingsPayload } from './broker_settings.js';

const broker = (id, family, mode, market = 'US') => ({ id, type: id, configuration_family: family,
  enabled: true, mode, execution_context: { market, asset_class: 'equity', currency: market === 'HK' ? 'HKD' : 'USD', timezone: market === 'HK' ? 'Asia/Hong_Kong' : 'America/New_York' } });

test('environment and market options only come from the selected module family', () => {
  const current = broker('a_paper', 'a', 'broker_paper');
  const live = broker('a_live', 'a', 'live');
  const hk = broker('a_hk', 'a', 'broker_paper', 'HK');
  const other = broker('b_paper', 'b', 'broker_paper');
  const disabled = { ...broker('disabled', 'a', 'live'), enabled: false };
  const choices = brokerChoices([current, live, hk, other, disabled], current);
  assert.deepEqual(choices.environments.map(item => item.id), ['a_paper', 'a_live']);
  assert.deepEqual(choices.markets.map(item => item.id), ['a_paper', 'a_hk']);
});

test('undeclared families are not combined by guessing broker names', () => {
  const first = { ...broker('new', 'a', 'broker_paper'), configuration_family: undefined };
  const second = { ...broker('new_live', 'a', 'live'), configuration_family: undefined };
  assert.equal(brokerChoices([first, second], first).environments.length, 1);
});

test('protected identity inputs are blank and payload stays bound to the edited revision', () => {
  const selected = { id: 'one', settings_revision: 'newer', fields: [
    { name: 'port', default: 123, type: 'integer' }, { name: 'account_id', secret: true },
  ], settings: { values: { port: 456, account_id: 'must-not-be-shown' } } };
  assert.deepEqual(settingsValues(selected), { port: 456, account_id: '' });
  assert.deepEqual(settingsPayload(selected, { port: 789, account_id: '', environment: 'REAL' }, 'edited'),
    { broker: 'one', revision: 'edited', values: { port: 789, account_id: '' } });
});
