import assert from 'node:assert/strict';
import test from 'node:test';
import { orderSide, orderStatus } from './orders.js';

test('abandoned plans and partial or unknown orders can never appear filled', () => {
  assert.equal(orderStatus('abandoned'), '已结束（未提交）');
  assert.equal(orderStatus('filled'), '已成交');
  for (const status of ['abandoned', 'partial', 'unknown', 'planned', 'accepted', 'pending', 'cancelled', 'rejected']) {
    assert.notEqual(orderStatus(status), orderStatus('filled'));
  }
  assert.equal(orderStatus('vendor-new-state'), 'vendor-new-state');
  assert.equal(orderStatus(null), '—');
});

test('buy and sell support either letter case while unfamiliar directions are preserved', () => {
  for (const side of ['BUY', 'buy']) assert.equal(orderSide(side), '买入');
  for (const side of ['SELL', 'sell']) assert.equal(orderSide(side), '卖出');
  assert.equal(orderSide('vendor-new-side'), 'vendor-new-side');
  assert.equal(orderSide(null), '—');
});
