import React, { useEffect, useRef, useState } from 'react';
import { api } from '../api';
import { Dialog } from './Dialog';
import { PythonEditor } from './PythonEditor';
import { editorBlockedReason, normalizeCode } from '../strategy_editor';
import { rememberDraft, recallDraft, forgetDraft } from '../strategy_editor_drafts';
import { downloadBlob } from '../strategy_instances';

export function StrategyEditorDialog({ strategyType, state, busy, act, onClose }) {
  const [file, setFile] = useState(null), [content, setContent] = useState('');
  const [loading, setLoading] = useState(true), [saving, setSaving] = useState(false);
  const [error, setError] = useState(''), [notice, setNotice] = useState(''), [discard, setDiscard] = useState(false);
  const savingRef = useRef(false);
  const strategy = state?.strategies.find(item => item.type === strategyType);
  const reason = editorBlockedReason(state, busy);
  const dirty = !!file && content !== file.content;
  useEffect(() => {
    let active = true;
    api(`strategy-plugins/${strategyType}/source`).then(value => {
      if (!active) return;
      const normalized = { ...value, content: normalizeCode(value.content) };
      const draft = recallDraft(strategyType);
      setFile(draft?.file || normalized); setContent(draft?.content ?? normalized.content);
      if (draft) {
        setNotice('已恢复当前标签页中未保存的修改。');
        if (draft.file.revision !== normalized.revision) setError('服务器文件已变化。草稿已保留，可复制或下载；重新打开最新文件后再合并修改。');
      }
    }).catch(failure => { if (active) setError(failure.message); })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [strategyType]);
  function close() { if (!savingRef.current) { if (dirty) setDiscard(true); else onClose(); } }
  function discardAndClose() { if (!savingRef.current) { forgetDraft(strategyType); onClose(); } }
  async function save() {
    if (!file || !dirty || reason || savingRef.current || discard) return;
    savingRef.current = true; setSaving(true); setError(''); setNotice('');
    const submitted = content;
    try {
      // Keep errors beside the source and never auto-retry a timed-out save.
      await act(async () => {
        try {
          const result = await api(`strategy-plugins/${strategyType}/source`, { content: submitted, revision: file.revision });
          setFile({ ...file, content: submitted, revision: result.revision });
          forgetDraft(strategyType);
          setNotice('已保存，原文件已备份。修改后的策略请重新回测。');
          return null;
        } catch (failure) { setError(failure.message); return null; }
      });
    } finally { savingRef.current = false; setSaving(false); }
  }
  return <Dialog labelledBy="strategy-editor-title" className="strategy-editor-dialog" onDismiss={close} canDismiss={!saving}>
    <div className="editor-heading"><div><h2 id="strategy-editor-title">{strategy?.name || strategyType} · Python</h2><p className="small muted">{file?.filename || '策略源文件'}</p></div><button className="button" disabled={saving} onClick={close}>关闭</button></div>
    {loading && <p role="status">正在打开代码…</p>}
    {reason && <p className="editor-notice" role="status">只读 · {reason}</p>}
    {error && <div className="alert error" role="alert">{error}</div>}
    {notice && <p className="editor-notice" role="status">{notice}</p>}
    {file && <PythonEditor initialValue={content} readOnly={!!reason || saving} onChange={value => { rememberDraft(strategyType, file, value); setContent(value); setNotice(''); setError(''); }} onSave={save}/>}
    <div className="editor-footer"><div><strong className="small">{dirty ? '有未保存的修改' : file ? '已与文件同步' : ''}</strong><p id="editor-help" className="small muted">Ctrl / ⌘ S 保存 · Ctrl / ⌘ F 搜索 · Tab 缩进 · 按 Esc 后再按 Tab 离开编辑区</p></div><div className="editor-buttons"><button className="button" disabled={!file} onClick={() => downloadBlob(new Blob([content], { type: 'text/x-python;charset=utf-8' }), `${strategyType}.py`)}>下载 .py</button><button className="button primary" disabled={!dirty || !!reason || saving || discard} onClick={save}>{saving ? '正在保存…' : '保存代码'}</button></div></div>
    {discard && <div className="editor-discard" role="alert"><span>关闭会丢弃这次尚未保存的修改。</span><div className="button-row"><button className="button" autoFocus disabled={saving} onClick={() => setDiscard(false)}>继续编辑</button><button className="button" disabled={saving} onClick={discardAndClose}>丢弃并关闭</button></div></div>}
  </Dialog>;
}
