import assert from 'node:assert/strict';
import test from 'node:test';
import { marketSession, newYorkClock } from './market.js';
import { applyPreferences, readPreference, savePreference } from './preferences.js';
import { relativeTime } from './format.js';
import { orderDay, orderNeedsAttention, orderStatus, orderSummary, orderTone } from './orders.js';

test('US session follows the New York clock across daylight saving changes', () => {
  // 2026-07-15 is EDT (UTC-4); 2026-12-15 is EST (UTC-5).
  assert.equal(marketSession(new Date('2026-07-15T13:00:00Z')).id, 'pre');
  assert.equal(marketSession(new Date('2026-07-15T13:30:00Z')).id, 'regular');
  assert.equal(marketSession(new Date('2026-12-15T14:29:00Z')).id, 'pre');
  assert.equal(marketSession(new Date('2026-12-15T14:30:00Z')).id, 'regular');
  assert.equal(marketSession(new Date('2026-07-15T20:00:00Z')).id, 'after');
  assert.equal(marketSession(new Date('2026-07-16T00:00:00Z')).id, 'closed');
  assert.equal(marketSession(new Date('2026-07-18T15:00:00Z')).label, '周末休市');
  assert.equal(newYorkClock(new Date('2026-07-15T13:30:05Z')), '09:30:05');
});

test('display preferences fall back safely when storage is missing, invalid or throws', () => {
  const store = new Map();
  const storage = { getItem: key => store.get(key) ?? null, setItem: (key, value) => store.set(key, value) };
  assert.equal(readPreference('theme', storage), 'dark');
  savePreference('theme', 'light', storage);
  assert.equal(readPreference('theme', storage), 'light');
  store.set('trading-portal:updown', 'purple');
  assert.equal(readPreference('updown', storage), 'us');
  const broken = { getItem() { throw new Error('blocked'); }, setItem() { throw new Error('blocked'); } };
  assert.equal(readPreference('theme', broken), 'dark');
  assert.equal(savePreference('updown', 'cn', broken), 'cn');
  assert.throws(() => savePreference('theme', 'neon', storage));
  const root = { dataset: {} };
  applyPreferences(root, storage);
  assert.deepEqual(root.dataset, { theme: 'light', updown: 'us' });
});

test('relative time reads naturally for upcoming and past moments', () => {
  const now = Date.parse('2026-10-07T12:00:00Z');
  assert.equal(relativeTime('2026-10-07T12:00:20Z', now), '即将开始');
  assert.equal(relativeTime('2026-10-07T11:59:50Z', now), '刚刚');
  assert.equal(relativeTime('2026-10-07T12:30:00Z', now), '约 30 分钟后');
  assert.equal(relativeTime('2026-10-07T14:10:00Z', now), '约 2 小时后');
  assert.equal(relativeTime('2026-10-04T12:00:00Z', now), '3 天前');
  assert.equal(relativeTime(null, now), '');
  assert.equal(relativeTime('not a date', now), '');
});

test('order views flag terminal and ambiguous orders and summarise the batch', () => {
  assert.equal(orderStatus('closed'), '已关闭（已核对）');
  for (const status of ['cancelled', 'rejected', 'unknown', 'partial', 'planned']) assert.equal(orderNeedsAttention(status), true, status);
  for (const status of ['filled', 'abandoned', 'closed']) assert.equal(orderNeedsAttention(status), false, status);
  assert.equal(orderTone('rejected'), 'red');
  assert.equal(orderTone('filled'), 'green');
  assert.equal(orderDay('ACC-1:2026-10-06'), '2026-10-06');
  assert.equal(orderDay(undefined), '—');
  assert.deepEqual(orderSummary([{ side: 'SELL', notional: '1000.5' }, { side: 'BUY', quantity: '3', limit_price: '10' }, { side: 'BUY', notional: 20 }]),
    { buy: 50, sell: 1000.5, buyCount: 2, sellCount: 1 });
});
