import assert from 'node:assert/strict';
import test from 'node:test';
import { marketSession, marketClock } from './market.js';
import { applyPreferences, readPreference, savePreference } from './preferences.js';
import { relativeTime } from './format.js';
import { orderDay, orderNeedsAttention, orderStatus, orderSummary, orderTone } from './orders.js';

test('market status comes from a fresh calendar observation and never a US clock guess', () => {
  const now = new Date('2026-10-10T03:00:00Z');
  assert.equal(marketSession({status:'open', observed_at:now.toISOString()}, now).label, '交易中');
  assert.equal(marketSession({status:'closed', observed_at:now.toISOString()}, now).id, 'closed');
  assert.equal(marketSession({status:'open', observed_at:'2026-10-09T03:00:00Z'}, now).id, 'unknown');
  assert.equal(marketSession(null, now).id, 'unknown');
  assert.equal(marketClock(now, 'Asia/Hong_Kong'), '11:00:00');
  assert.equal(marketClock(new Date('2026-07-15T13:30:05Z'), 'America/New_York'), '09:30:05');
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
  assert.deepEqual(orderSummary([{ side: 'SELL', notional: '1000.5' }, { side: 'BUY', quantity: '3', limit_price: '10', contract_multiplier:'1' }, { side: 'BUY', notional: 20 }]),
    { buy: 50, sell: 1000.5, buyCount: 2, sellCount: 1 });
});


test('a throwing localStorage getter cannot prevent the initial page from rendering', () => {
  const original = Object.getOwnPropertyDescriptor(globalThis, 'localStorage');
  Object.defineProperty(globalThis, 'localStorage', { configurable: true, get() { throw new Error('SecurityError'); } });
  try {
    assert.equal(readPreference('theme'), 'dark');
    assert.equal(savePreference('theme', 'light'), 'light');
    const root = { dataset: {} };
    applyPreferences(root);
    assert.deepEqual(root.dataset, { theme: 'dark', updown: 'us' });
  } finally {
    if (original) Object.defineProperty(globalThis, 'localStorage', original);
    else delete globalThis.localStorage;
  }
});

test('display toggles still work together when storing settings is blocked', () => {
  const blocked = { getItem() { throw new Error('blocked'); }, setItem() { throw new Error('blocked'); } };
  const root = { dataset: { theme: 'dark', updown: 'us' } };
  applyPreferences(root, blocked, { ...root.dataset, theme: savePreference('theme', 'light', blocked) });
  assert.deepEqual(root.dataset, { theme: 'light', updown: 'us' });
  applyPreferences(root, blocked, { ...root.dataset, updown: savePreference('updown', 'cn', blocked) });
  assert.deepEqual(root.dataset, { theme: 'light', updown: 'cn' });
});
