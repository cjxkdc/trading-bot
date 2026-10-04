import assert from 'node:assert/strict';
import test from 'node:test';
import { runtimeView } from './runtime.js';

test('a lost heartbeat cannot display a green running state', () => {
  const view = runtimeView({ status: 'running', alive: true }, false, false, null);
  assert.equal(view.status, 'unknown');
  assert.equal(view.tone, 'warning');
  assert.equal(view.disabled, true);
});

test('off can start; a busy or unresponsive process cannot be stopped or duplicated', () => {
  assert.equal(runtimeView({ status: 'stopped', alive: false }, true, false, null).disabled, false);
  assert.equal(runtimeView({ status: 'running', alive: true }, true, false, null).disabled, false);
  assert.equal(runtimeView({ status: 'running', alive: true }, true, false, { action: 'execute' }).disabled, true);
  assert.equal(runtimeView({ status: 'unresponsive', alive: true }, true, false, null).disabled, true);
});

test('starting and stopping are transitions, never a completed off state', () => {
  for (const status of ['starting', 'stopping']) {
    const view = runtimeView({ status, alive: true }, true, false, null);
    assert.equal(view.status, status);
    assert.equal(view.checked, true);
    assert.equal(view.disabled, true);
  }
});
