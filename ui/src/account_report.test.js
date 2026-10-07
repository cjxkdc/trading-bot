import test from 'node:test';
import assert from 'node:assert/strict';
import { chartLayout, visiblePositions, money, rate } from './account_report.js';

test('unreviewed return samples are not drawn as zero or confused with equity', () => {
  const points = [{ as_of: '2026-10-01T20:00:00Z', equity: 1000, return_rate: 0 }, { as_of: '2026-10-02T20:00:00Z', equity: 100000, return_rate: null }];
  assert.equal(chartLayout(points, 'return_rate').dots.length, 1);
  assert.equal(chartLayout(points, 'equity').dots.length, 2);
  assert.equal(chartLayout([{ ...points[0], return_rate: null }], 'return_rate'), null);
  assert.equal(money(null), '—');
  assert.equal(rate(NaN), '—');
});

test('flat or single observation paths stay finite and use actual time spacing', () => {
  const points = [1, 2, 5].map(day => ({ as_of: `2026-10-0${day}T20:00:00Z`, equity: 1000 }));
  const layout = chartLayout(points, 'equity', 400);
  assert.ok(layout.dots.every(p => Number.isFinite(p.x) && Number.isFinite(p.y)));
  assert.equal((layout.dots[1].x - layout.dots[0].x) * 3, layout.dots[2].x - layout.dots[1].x);
  assert.ok(Number.isFinite(chartLayout(points.slice(0, 1), 'equity').dots[0].y));
});

test('holdings sort puts missing PnL last and excludes sold target-only rows without mutating evidence', () => {
  const rows = [{ symbol: 'A', name: 'Apple', quantity: 1, position_pnl: null }, { symbol: 'B', quantity: 1, position_pnl: -3 }, { symbol: 'C', quantity: 0, position_pnl: 100 }];
  assert.deepEqual(visiblePositions(rows, '', 'position_pnl').map(p => p.symbol), ['B', 'A']);
  assert.deepEqual(visiblePositions(rows, ' apple ', 'position_pnl').map(p => p.symbol), ['A']);
  assert.equal(rows[0].symbol, 'A');
});
