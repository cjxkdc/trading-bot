import React, { useRef, useState } from 'react';
import { api } from '../api';
import { downloadBlob } from '../strategy_instances';

export function StrategyFiles({ state, busy, act }) {
  const [type, setType] = useState(state.strategy.type), [file, setFile] = useState(null), [content, setContent] = useState('');
  const input = useRef(null);
  const canEdit = !busy && !state.automation?.enabled && !state.strategy_manager?.running_count && !state.strategy_manager?.instances.some(x => x.pending);
  async function read() {
    await act(async () => { const value = await api(`strategy-plugins/${type}/source`); setFile(value); setContent(value.content); return null; });
  }
  async function upload(selected) {
    if (!selected) return;
    const result = await act(() => api('strategy-plugins/upload', selected, { raw: true }));
    if (result) { setType(result.type); setFile(null); }
  }
  async function save() {
    const result = await act(() => api(`strategy-plugins/${type}/source`, { content, revision: file.revision }));
    if (result) setFile({ ...file, content, revision: result.revision });
  }
  return <details className="card details strategy-file-panel"><summary>策略文件 · 上传、下载与编辑</summary>
    <div className="instance-export"><label>策略模块<select value={type} disabled={busy} onChange={e => { setType(e.target.value); setFile(null); }}>{state.strategies.map(x => <option key={x.type} value={x.type}>{x.name}</option>)}</select></label>
      <button className="button" disabled={busy} onClick={read}>打开 Python 文件</button>
      <button className="button" disabled={busy} onClick={() => act(async () => { await downloadBlob(await api(`strategy-plugins/${type}/bundle`, undefined, { download: true }), `${type}.zip`); return null; })}>下载策略包</button>
      <button className="button" disabled={!canEdit} onClick={() => input.current?.click()}>上传策略包</button>
    </div><p className="small muted">ZIP 包包含 strategy.py、manifest.json，可选 README.md；采用组合策略接口。上传与编辑前先停止所有实例，保存时自动备份原代码。</p>
    <input hidden ref={input} type="file" accept=".zip,application/zip" onChange={e => { upload(e.target.files?.[0]); e.target.value = ''; }}/>
    {file && <><p className="small muted">{file.filename}</p><textarea className="strategy-code-editor" aria-label="Python 策略代码" spellCheck={false} value={content} disabled={!canEdit} onChange={e => setContent(e.target.value)} onKeyDown={e => { if (e.key === 'Tab') { e.preventDefault(); const start = e.target.selectionStart; const end = e.target.selectionEnd; setContent(content.slice(0, start) + '    ' + content.slice(end)); const element = e.target; requestAnimationFrame(() => { element.selectionStart = element.selectionEnd = start + 4; }); } }}/><div className="form-footer"><span className="small muted">保存会检查语法与文件版本，修改后请重新回测。</span><button className="button primary" disabled={!canEdit || content === file.content} onClick={save}>保存代码</button></div></>}
  </details>;
}
