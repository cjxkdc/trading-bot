import assert from 'node:assert/strict';
import test from 'node:test';
import { normalizeCode, editorBlockedReason } from './strategy_editor.js';
import { rememberDraft, recallDraft, forgetDraft } from './strategy_editor_drafts.js';

const state = () => ({ automation: { enabled: false }, strategy_manager: { instances: [], accounts: [], running_count: 0 } });
test('re-login drafts remain scoped to strategy and retain the original revision', () => {
  const file = { filename: 'alpha.py', revision: 'old', content: 'original' };
  rememberDraft('alpha', file, 'edited');
  file.revision = 'new';
  assert.equal(recallDraft('alpha').file.revision, 'old');
  assert.equal(recallDraft('alpha').content, 'edited');
  assert.equal(recallDraft('beta'), undefined);
  rememberDraft('alpha', file, 'original');
  assert.equal(recallDraft('alpha'), undefined);
  rememberDraft('beta', file, 'edited'); forgetDraft('beta');
  assert.equal(recallDraft('beta'), undefined);
});
test('editor normalizes platform newlines without changing Python indentation or Unicode', () => {
  assert.equal(normalizeCode('class Alpha:\r\n    # 中文\r    pass\n'), 'class Alpha:\n    # 中文\n    pass\n');
});
test('editor explains active, pending and unreadable runtime state', () => {
  assert.equal(editorBlockedReason(state(), false), '');
  for (const change of [s => s.automation.enabled = true, s => s.strategy_manager.instances.push({enabled: true}), s => s.strategy_manager.running_count = 1]) {
    const value = state(); change(value); assert.match(editorBlockedReason(value, false), /暂停/);
  }
  for (const collection of ['instances', 'accounts']) {
    const value = state(); value.strategy_manager[collection].push({pending: true}); assert.match(editorBlockedReason(value, false), /订单批次/);
  }
  for (const area of ['automation', 'strategy_manager']) {
    const value = state(); value[area].corrupt = true; assert.match(editorBlockedReason(value, false), /运行记录/);
  }
  assert.ok(editorBlockedReason(null, false));
  assert.ok(editorBlockedReason(state(), true));
});
