import React, { useRef, useState } from 'react';
import { api } from '../api';
import { downloadBlob } from '../strategy_instances';
import { editorBlockedReason } from '../strategy_editor';

export function StrategyFiles({ state, busy, act, onEdit }) {
  const [type, setType] = useState(state.strategy.type);
  const input = useRef(null);
  const canEdit = !editorBlockedReason(state, busy);
  async function upload(selected) {
    if (!selected) return;
    const result = await act(() => api('strategy-plugins/upload', selected, { raw: true }));
    if (result) setType(result.type);
  }
  return <details className="card details strategy-file-panel"><summary>策略文件 · 导入与导出</summary>
    <div className="instance-export"><label>策略模块<select value={type} disabled={busy} onChange={e => setType(e.target.value)}>{state.strategies.map(x => <option key={x.type} value={x.type}>{x.name}</option>)}</select></label>
      <button className="button" disabled={busy} onClick={() => onEdit(type)}>查看 / 编辑 Python</button>
      <button className="button" disabled={busy} onClick={() => act(async () => { await downloadBlob(await api(`strategy-plugins/${type}/bundle`, undefined, { download: true }), `${type}.zip`); return null; })}>下载策略包</button>
      <button className="button" disabled={!canEdit} onClick={() => input.current?.click()}>上传策略包</button>
    </div><p className="small muted">ZIP 包包含 strategy.py、manifest.json，可选 README.md；采用组合策略接口。上传与编辑前先停止所有实例，保存时自动备份原代码。</p>
    <input hidden ref={input} type="file" accept=".zip,application/zip" onChange={e => { upload(e.target.files?.[0]); e.target.value = ''; }}/>
  </details>;
}
