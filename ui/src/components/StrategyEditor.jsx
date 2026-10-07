import React, { useEffect, useRef, useState } from 'react';
import { api } from '../api';
import { PythonEditor } from './PythonEditor';
import { editorBlockedReason, normalizeCode } from '../strategy_editor';
import { rememberDraft, recallDraft, forgetDraft } from '../strategy_editor_drafts';
import { downloadBlob } from '../strategy_instances';

export function StrategyEditor({ strategyType, state, busy, act, onDraftChange }) {
  const [file, setFile] = useState(null), [content, setContent] = useState('');
  const [loading, setLoading] = useState(true), [saving, setSaving] = useState(false);
  const [error, setError] = useState(''), [notice, setNotice] = useState(''), [discard, setDiscard] = useState(false);
  const [editorKey, setEditorKey] = useState(0);
  const savingRef = useRef(false), mounted = useRef(false);
  const reason = editorBlockedReason(state, busy);
  const dirty = !!file && content !== file.content;
  useEffect(() => {
    let active = true;
    mounted.current = true;
    api(`strategy-plugins/${strategyType}/source`).then(value => {
      if (!active) return;
      const normalized = { ...value, content: normalizeCode(value.content) };
      const draft = recallDraft(strategyType);
      setFile(draft?.file || normalized); setContent(draft?.content ?? normalized.content);
      if (draft) {
        setNotice('已恢复此标签页中的草稿。');
        if (draft.file.revision !== normalized.revision) setError('服务器代码已变化。请先下载草稿，再载入最新文件并合并修改。');
      }
    }).catch(failure => { if (active) setError(failure.message); })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; mounted.current = false; };
  }, [strategyType]);
  async function reload() {
    if (savingRef.current) return;
    setLoading(true); setError('');
    try {
      const value = await api(`strategy-plugins/${strategyType}/source`);
      if (!mounted.current) return;
      const normalized = { ...value, content: normalizeCode(value.content) };
      forgetDraft(strategyType); onDraftChange();
      setFile(normalized); setContent(normalized.content); setEditorKey(k => k + 1);
      setNotice('已载入最新文件。'); setDiscard(false);
    } catch (failure) { if (mounted.current) setError(failure.message); }
    finally { if (mounted.current) setLoading(false); }
  }
  async function save() {
    if (!file || !dirty || reason || loading || savingRef.current || discard) return;
    savingRef.current = true; setSaving(true); setError(''); setNotice('');
    const submitted = content;
    try {
      await act(async () => {
        try {
          const result = await api(`strategy-plugins/${strategyType}/source`, { content: submitted, revision: file.revision });
          forgetDraft(strategyType); onDraftChange();
          if (mounted.current) {
            setFile({ ...file, content: submitted, revision: result.revision });
            setNotice('已保存，原文件已备份。修改后的策略请重新回测。');
          }
          return null;
        } catch (failure) { if (mounted.current) setError(failure.message); return null; }
      });
    } finally { savingRef.current = false; if (mounted.current) setSaving(false); }
  }
  return <section className="strategy-code-panel" aria-label="策略 Python 编辑器">
    <div className="source-toolbar"><div><strong>{file?.filename || 'strategy.py'}</strong><span className={dirty ? 'draft-indicator' : 'muted'}>{dirty ? '未保存' : file ? '已保存' : '正在读取'}</span></div><div className="editor-buttons"><button className="button" disabled={!file || loading || saving} onClick={() => dirty ? setDiscard(true) : reload()}>重新载入</button><button className="button" disabled={!file} onClick={() => downloadBlob(new Blob([content], { type: 'text/x-python;charset=utf-8' }), `${strategyType}.py`)}>下载 .py</button><button className="button primary" disabled={!dirty || !!reason || loading || saving || discard} onClick={save}>{saving ? '保存中…' : '保存代码'}</button></div></div>
    {reason && !saving && <p className="editor-notice" role="status">只读 · {reason}</p>}
    {error && <div className="alert error" role="alert">{error}</div>}
    {notice && <p className="editor-notice" role="status">{notice}</p>}
    {discard && <div className="editor-discard" role="alert"><span>载入最新文件会丢弃当前草稿。</span><div className="button-row"><button className="button" autoFocus disabled={loading} onClick={() => setDiscard(false)}>继续编辑</button><button className="button" disabled={loading} onClick={reload}>丢弃并载入</button></div></div>}
    {loading && <p role="status">正在读取代码…</p>}
    {file && <PythonEditor key={editorKey} initialValue={content} readOnly={!!reason || saving || loading} onChange={value => { rememberDraft(strategyType, file, value); setContent(value); onDraftChange(); setNotice(''); }} onSave={save}/>}
    <p id="editor-help" className="editor-shortcuts">Ctrl / ⌘ S 保存 · Ctrl / ⌘ F 搜索 · Tab 缩进 · Esc 后再按 Tab 离开编辑区</p>
  </section>;
}
