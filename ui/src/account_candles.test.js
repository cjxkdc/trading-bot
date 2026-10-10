import test from 'node:test';
import assert from 'node:assert/strict';
import { candleLayout, nearestCandle, moveCandle, candleLabel } from './account_candles.js';

const bar = (hour=14,extra={}) => ({start:`2025-01-02T${hour}:00:00Z`,end:`2025-01-02T${hour+1}:00:00Z`,open:100,high:120,low:90,close:110,sample_count:4,...extra});

test('flat one-point observations render at finite coordinates without invented movement',()=>{
  const layout=candleLayout([bar(14,{open:100,high:100,low:100,close:100,sample_count:1})]);
  const p=layout.points[0];
  assert.equal(p.tone,'flat');
  assert.equal(p.yOpen,p.yHigh); assert.equal(p.yOpen,p.yClose);
  assert.ok([p.x,p.width,p.yOpen,...layout.ticks.map(t=>t.value)].every(Number.isFinite));
});

test('gaps occupy real time on the axis and high/low are positioned correctly',()=>{
  const layout=candleLayout([bar(14),bar(15),bar(18)]);
  const [a,b,c]=layout.points;
  assert.ok(Math.abs((c.x-b.x)/(b.x-a.x)-3)<1e-10);
  assert.ok(a.yHigh<a.yClose && a.yClose<a.yOpen && a.yOpen<a.yLow);
});

test('invalid financial values, inconsistent extrema and overlapping windows are rejected',()=>{
  for(const extra of [{low:0},{open:NaN},{high:105},{low:101},{sample_count:0},{sample_count:1.5},{start:'bad'}])
    assert.equal(candleLayout([bar(14,extra)]),null);
  assert.equal(candleLayout([bar(14),bar(14)]),null);
  assert.equal(candleLayout([]),null);
});

test('pointer and keyboard selection work at boundaries and through time gaps',()=>{
  const points=candleLayout([bar(14),bar(15),bar(18)]).points;
  assert.equal(nearestCandle(points,points[1].x+1),1);
  assert.equal(nearestCandle([],0),null);
  assert.equal(moveCandle(null,'ArrowLeft',3),1);
  assert.equal(moveCandle(0,'ArrowLeft',3),0);
  assert.equal(moveCandle(2,'ArrowRight',3),2);
  assert.equal(moveCandle(1,'Home',3),0);
  assert.equal(moveCandle(0,'End',3),2);
});

test('readout uses New York dates and distinguishes repeated daylight-saving hours',()=>{
  const a=candleLabel({start:'2025-11-02T05:00:00Z'},'1h','America/New_York');
  const b=candleLabel({start:'2025-11-02T06:00:00Z'},'1h','America/New_York');
  assert.notEqual(a,b);
  assert.match(candleLabel({start:'2025-01-06T03:00:00Z'},'1d','America/New_York'),/2025.*01.*05/);
  const week=candleLabel({start:'2025-01-06T05:00:00Z',end:'2025-01-13T05:00:00Z'},'1w','America/New_York');
  assert.match(week,/2025.*01.*06.*2025.*01.*12/);
});


test('HK and unknown reporting timezones do not inherit US dates', () => {
  assert.match(candleLabel({start:'2025-01-06T03:00:00Z'}, '1d', 'Asia/Hong_Kong'), /2025.*01.*06/);
  assert.equal(candleLabel({start:'2025-01-06T03:00:00Z'}, '1d'), '—');
  assert.equal(candleLabel({start:'2025-01-06T03:00:00Z'}, '1d', 'bad/timezone'), '—');
});
