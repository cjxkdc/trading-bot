import React, { useEffect, useState } from 'react';
import { api } from '../api';
import { Button, Empty } from '../ui';
import { cls, time } from '../format';

const names = [['backend', '交易后台 · backend.log'], ['portal', '面板 · portal.log']];

function formatSize(bytes) {
  if (!bytes) return '空';
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

// Diagnostic logs are read through the control plane, so they stay available
// while the trading backend is stopped. Error banners carry a 诊断编号; pasting
// it here finds the matching record.
export function LogViewer({ initialQuery = '' }) {
  const [name, setName] = useState('backend');
  const [query, setQuery] = useState(initialQuery);
  const [applied, setApplied] = useState(initialQuery);
  const [reload, setReload] = useState(0);
  const [data, setData] = useState(null);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  const [open, setOpen] = useState({});
  useEffect(() => { setQuery(initialQuery); setApplied(initialQuery); }, [initialQuery]);
  useEffect(() => {
    let live = true;
    setLoading(true);
    api(`runtime/logs?name=${encodeURIComponent(name)}&limit=200&query=${encodeURIComponent(applied)}`)
      .then(value => { if (live) { setData(value); setError(''); } })
      .catch(failure => { if (live) setError(failure.message); })
      .finally(() => { if (live) setLoading(false); });
    return () => { live = false; };
  }, [name, applied, reload]);
  const records = data?.records || [];
  return <section className="card log-viewer" aria-label="诊断日志">
    <div className="card-heading"><div><h2>诊断日志</h2><p>只记录异常类型、代码位置和请求路径，不含券商返回的原文。错误提示里的「诊断编号」可以在这里搜索。</p></div></div>
    <form className="log-toolbar" onSubmit={event => { event.preventDefault(); setApplied(query.trim()); }}>
      <label>日志文件<select value={name} onChange={event => setName(event.target.value)}>{names.map(([id, label]) => <option key={id} value={id}>{label}</option>)}</select></label>
      <label>诊断编号或关键字<input value={query} placeholder="例如 3f9a1c2b7d" maxLength="80" onChange={event => setQuery(event.target.value)}/></label>
      <div className="button-row"><Button type="submit" disabled={loading}>搜索</Button><Button type="button" disabled={loading} icon="refresh" onClick={() => setReload(value => value + 1)}>刷新</Button></div>
    </form>
    {data && <p className="small muted log-files">{data.folder}/ · {data.files.map(file => `${file.name}.log ${formatSize(file.size)}`).join(' · ')}{data.truncated ? ' · 只读取了文件末尾' : ''}</p>}
    {error && <p className="inline-warning" role="alert">{error}</p>}
    {!error && data && !records.length && <Empty title={applied ? '没有匹配的记录' : '日志为空'} icon="list">{applied ? '换一个编号或关键字，或者切换另一个日志文件。' : '只有出错时才会写入；正常运行不产生记录。'}</Empty>}
    {records.length > 0 && <div className="log-records">
      {records.map((record, index) => <div className={cls('log-record', open[index] && 'open')} key={index}>
        <div className="log-record-header"><code>{record.header}</code>{record.lines.length > 0 && <button type="button" className="text-link" onClick={() => setOpen({ ...open, [index]: !open[index] })}>{open[index] ? '收起' : `展开 ${record.lines.length} 行`}</button>}</div>
        {open[index] && record.lines.length > 0 && <pre className="log-record-body">{record.lines.join('\n')}</pre>}
      </div>)}
    </div>}
    {data?.files?.some(file => file.modified) && <p className="small muted">最近写入：{time(data.files.filter(file => file.modified).sort((a, b) => Date.parse(b.modified) - Date.parse(a.modified))[0].modified)}</p>}
  </section>;
}
