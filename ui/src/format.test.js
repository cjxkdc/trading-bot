import test from 'node:test';
import assert from 'node:assert/strict';
import { decimal, number } from './format.js';

test('instrument quantities and prices retain fractional precision independently of money', () => {
  for (const value of ['0.001',0.001]) assert.equal(decimal(value), '0.001');
  for (const value of ['0.00000001',0.00000001,'1e-8']) assert.equal(decimal(value), '0.00000001');
  assert.equal(decimal('0.00001230'), '0.00001230');
  assert.equal(decimal('-0.001'), '-0.001');
  assert.equal(decimal('.125'), '0.125');
  assert.equal(number('12.3456'), '12.35');
});

test('large integer and decimal strings never lose precision through Number', () => {
  assert.equal(decimal('9007199254740993'), '9,007,199,254,740,993');
  assert.equal(decimal('12345678901234567890.00000001'), '12,345,678,901,234,567,890.00000001');
  assert.equal(decimal('9007199254740993e2'), '900,719,925,474,099,300');
  assert.equal(decimal('0012.3000'), '12.3000');
  assert.equal(decimal('-0'), '0');
});

test('missing malformed and numerically unsafe inputs are never displayed as zero', () => {
  for (const value of [null,undefined,true,NaN,Infinity,'','bad','1,000','0x10','1e999999',9007199254740992]) assert.equal(decimal(value), '—');
});
