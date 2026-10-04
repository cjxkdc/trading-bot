import assert from 'node:assert/strict';
import test from 'node:test';
import { awsLink, capacityMetric, formatBytes, formatUptime, freshTimestamp, isServerDeployment, nextReceipt, percentMetric, serverFresh, serverHealth, serverOperations } from './server.js';

const now = Date.parse('2026-10-03T10:00:00Z');
const report = { checked_at: '2026-10-03T10:00:00Z', portal: { status: 'running' }, runtime: { status: 'running' },
  backend: { reachable: true, active_job: false, automation_enabled: false, automation_status: 'disabled' },
  operations: [{ id: 'start', enabled: false }, { id: 'stop', enabled: true }] };

test('only server deployments expose the server navigation', () => {
  assert.equal(isServerDeployment({ deployment: 'server' }), true);
  assert.equal(isServerDeployment({ enabled: true }), true);
  assert.equal(isServerDeployment({ deployment: 'desktop', enabled: false }), false);
  assert.equal(isServerDeployment(null), false);
});

test('freshness uses receipt elapsed time and accepts client clocks ahead of or behind AWS', () => {
  assert.equal(serverFresh(report, now, now + 10000), true);
  assert.equal(serverFresh(report, now, now + 31000), false);
  for (const offset of [-300000, 47000, 300000]) {
    const skewedReport = { checked_at: new Date(now + offset).toISOString() };
    assert.equal(serverFresh(skewedReport, nextReceipt(null, skewedReport, 0, now), now + 10000), true);
  }
  assert.equal(serverFresh({ checked_at: 'not-a-date' }, now, now), false);
  assert.equal(serverFresh(report, 0, now), false);
  assert.equal(serverFresh(report, now, now - 1), false);
});

test('repeated cached samples cannot renew freshness and regression requires a new baseline', () => {
  let receipt = nextReceipt(null, report, 0, now);
  for (const seconds of [10, 20, 40]) receipt = nextReceipt(report, report, receipt, now + seconds * 1000);
  assert.equal(receipt, now);
  assert.equal(serverFresh(report, receipt, now + 40000), false);
  const advanced = { checked_at: '2026-10-03T10:00:45Z' };
  assert.equal(nextReceipt(report, advanced, receipt, now + 45000), now + 45000);
  const regressed = { checked_at: '2026-10-03T09:59:59Z' };
  assert.equal(nextReceipt(report, regressed, receipt, now), 0);
  assert.equal(nextReceipt(report, { checked_at: 'invalid' }, receipt, now), 0);
  assert.equal(nextReceipt(report, report, 0, now + 10000), 0);
});

test('host samples are compared with the server report clock, not the client clock', () => {
  const serverNow = now + 47000;
  assert.equal(freshTimestamp(new Date(serverNow - 1000).toISOString(), serverNow), true);
  assert.equal(freshTimestamp(new Date(serverNow - 31000).toISOString(), serverNow), false);
  assert.equal(freshTimestamp(new Date(serverNow + 6000).toISOString(), serverNow), false);
  assert.equal(freshTimestamp(null, serverNow), false);
});

test('unknown, stale and unreachable services never appear healthy', () => {
  assert.equal(serverHealth(report, true).portal.tone, 'green');
  assert.equal(serverHealth(report, true).backend.tone, 'green');
  assert.equal(serverHealth(report, true).automation.label, '已关闭');
  for (const health of Object.values(serverHealth(report, false))) assert.equal(health.tone, 'muted');
  const unreachable = { ...report, backend: { ...report.backend, reachable: false, automation_enabled: true } };
  assert.notEqual(serverHealth(unreachable, true).backend.tone, 'green');
  assert.equal(serverHealth(unreachable, true).automation.label, '状态未知');
  assert.equal(serverHealth({ ...report, runtime: { status: 'stopped' } }, true).backend.label, '已停止');
});

test('start and stop require current server permission; active work always blocks stop', () => {
  assert.equal(serverOperations(report, { fresh: true })[1].enabled, true);
  for (const options of [{ fresh: false }, { fresh: true, pending: true }]) {
    assert.ok(serverOperations(report, options).every(operation => !operation.enabled));
  }
  assert.equal(serverOperations({ ...report, backend: { active_job: true } }, { fresh: true })[1].enabled, false);
  assert.ok(serverOperations({ ...report, operations: [] }, { fresh: true }).every(operation => !operation.enabled));
  const stopped = { ...report, runtime: { status: 'stopped' }, operations: [{ id: 'start', enabled: true }] };
  assert.equal(serverOperations(stopped, { fresh: true })[0].enabled, true);
  assert.equal(serverOperations(stopped, { fresh: true })[1].enabled, false);
});

test('missing metrics remain unknown while real zero usage remains zero', () => {
  for (const value of [null, undefined, NaN, Infinity, -1, '0']) assert.equal(formatBytes(value), '—');
  assert.equal(formatBytes(1024 ** 3), '1 GiB');
  assert.equal(percentMetric(0).label, '0%');
  assert.equal(percentMetric(null).value, null);
  assert.equal(capacityMetric(0, 1024).label, '0%');
  for (const [used, total] of [[null, 100], [0, 0], [200, 100], [-1, 100]]) {
    assert.equal(capacityMetric(used, total).value, null);
  }
  assert.equal(formatUptime(90061), '1 天 1 小时');
  assert.equal(formatUptime(null), '—');
});

test('management links accept observed official AWS HTTPS URLs only', () => {
  assert.equal(awsLink('https://lightsail.aws.amazon.com/ls/webapp/home/instances'), 'https://lightsail.aws.amazon.com/ls/webapp/home/instances');
  assert.equal(awsLink('https://123.ap-southeast-2.console.aws.amazon.com/lightsail/remote'), 'https://123.ap-southeast-2.console.aws.amazon.com/lightsail/remote');
  for (const value of [null, 'javascript:alert(1)', 'http://lightsail.aws.amazon.com/',
    'https://lightsail.aws.amazon.com.evil.example/', 'https://evil.example/', 'https://user:secret@console.aws.amazon.com/']) {
    assert.equal(awsLink(value), null);
  }
});
