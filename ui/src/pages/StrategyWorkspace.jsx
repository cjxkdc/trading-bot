import React, { lazy, Suspense, useRef, useState } from 'react';
import { api } from '../api';
import { ModuleFields } from '../components/ModuleFields';
import { ReadmeMarkdown } from '../readme';
import { editorBlockedReason } from '../strategy_editor';
import { recallDraft } from '../strategy_editor_drafts';
import { downloadBlob, instanceStatus } from '../strategy_instances';
import { ResearchPage } from './ResearchPage';
import { percent } from '../format';

const Editor = lazy(() => import('../components/StrategyEditor').then(m => ({ default: m.StrategyEditor })));
const tabs = [['readme', '策略说明'], ['parameters', '参数'], ['code', 'Python 代码'], ['research', '回测实验'], ['runs', '运行记录']];

export function StrategyWorkspace({ state, busy, act, route, navigate }) {
  const input = useRef(null);
  const [, updateDrafts] = useState(0);
  const [search, setSearch] = useState('');
  const [parameterDrafts, setParameterDrafts] = useState({});
  const selected = route.strategy ? state.strategies.find(s => s.type === route.strategy) : state.strategies.find(s => s.type === state.strategy.type) || state.strategies[0];
  const tab = route.tab || 'readme';
  const reason = editorBlockedReason(state, busy);
  const list = state.strategies.filter(s => `${s.name} ${s.type}`.toLowerCase().includes(search.trim().toLowerCase()));
  const instances = state.strategy_manager?.instances?.filter(s => s.strategy?.type === selected?.type) || [];
  function select(type, nextTab = tab) { navigate({ page: 'strategies', strategy: type, tab: nextTab }); }
  async function upload(file) {
    if (!file) return;
    const result = await act(() => api('strategy-plugins/upload', file, { raw: true }));
    if (result?.type) select(result.type, 'readme');
  }
  return <div className="strategy-workspace">
    <aside className="strategy-library" aria-label="策略库">
      <div className="library-heading"><h2>策略库 <small>{state.strategies.length}</small></h2><button className="text-link" disabled={!!reason} title={reason || '导入含 manifest.json 与 strategy.py 的 ZIP'} onClick={() => input.current?.click()}>导入</button></div>
      <input type="search" aria-label="搜索策略" placeholder="搜索策略" value={search} onChange={e => setSearch(e.target.value)}/>
      <nav aria-label="选择策略">{list.map(s => <button key={s.type} className={`library-strategy ${selected?.type === s.type ? 'selected' : ''}`} aria-pressed={selected?.type === s.type} onClick={() => select(s.type)}><strong>{s.name}</strong><span>{s.type}{recallDraft(s.type) && <i title="有未保存的代码">草稿</i>}</span><small>{s.type === state.strategy.type ? '手动配置' : s.enabled ? `v${s.version}` : '暂不可用'}</small></button>)}{!list.length && <p className="small muted">没有匹配的策略</p>}</nav>
      <p className="library-help">策略包可单独导入、修改和导出。</p>
      <input hidden ref={input} type="file" accept=".zip,application/zip" onChange={e => { upload(e.target.files?.[0]); e.target.value = ''; }}/>
    </aside>
    {!selected ? <p>{route.strategy ? '这个策略已不存在，请从左侧重新选择。' : '导入一个策略包即可开始。'}</p> : <div className="strategy-work-area">
      <div className="workspace-heading"><div><h2>{selected.name}</h2><p>v{selected.version} · {selected.type}{instances.length > 0 && ` · ${instances.length} 个运行实例`}</p></div><button className="button" disabled={busy} onClick={() => act(async () => { downloadBlob(await api(`strategy-plugins/${selected.type}/bundle`, undefined, { download: true }), `${selected.type}.zip`); return null; })}>导出策略包</button></div>
      <div className="workspace-tabs" role="tablist" aria-label="策略工作区">{tabs.map(([id, name]) => <button key={id} role="tab" id={`strategy-tab-${id}`} aria-selected={tab === id} aria-controls={`strategy-panel-${id}`} onClick={() => select(selected.type, id)}>{name}{id === 'code' && recallDraft(selected.type) && <span className="tab-draft" aria-label="有未保存代码"/>}</button>)}</div>
      {!selected.enabled && <p className="inline-warning">{selected.reason}</p>}
      <section className="workspace-content" role="tabpanel" id={`strategy-panel-${tab}`} aria-labelledby={`strategy-tab-${tab}`}>
        {tab === 'research' ? <ResearchPage key={selected.type} strategyType={selected.type} state={state} busy={busy} act={act} navigate={navigate}/> : tab === 'runs' ? <StrategyDeployments strategy={selected} instances={instances} state={state} busy={busy} navigate={navigate}/> : tab === 'code' ? <Suspense fallback={<p role="status">正在打开编辑器…</p>}><Editor key={selected.type} strategyType={selected.type} state={state} busy={busy} act={act} onDraftChange={() => updateDrafts(n => n + 1)}/></Suspense> : tab === 'parameters' ? <StrategyParameters key={selected.type} strategy={selected} state={state} busy={busy} act={act} draft={parameterDrafts[selected.type]} setDraft={value => setParameterDrafts(prev => ({ ...prev, [selected.type]: value }))}/> : <>
          {selected.readme_issue ? <p className="inline-warning">{selected.readme_issue}</p> : selected.readme?.content ? <div className="readme-markdown workspace-readme"><ReadmeMarkdown content={selected.readme.content}/></div> : <div className="readme-empty"><h3>还没有策略说明</h3><p>在策略包中加入 README.md，便可在这里查看交易规则和使用说明。</p><button className="text-link" onClick={() => select(selected.type, 'code')}>查看 Python 代码 →</button></div>}
          <div className="strategy-next"><span>策略定义交易规则，运行实例管理账户、资金和调仓计划。</span><div className="button-row"><button className="text-link" onClick={() => select(selected.type, 'research')}>查看回测实验 →</button><button className="text-link" onClick={() => select(selected.type, 'runs')}>查看运行记录 →</button></div></div>
        </>}
      </section>
    </div>}
  </div>;
}

function StrategyParameters({ strategy, state, busy, act, draft, setDraft }) {
  const initial = state.strategy.type === strategy.type ? state.strategy : { type: strategy.type, ...strategy.defaults };
  const values = draft?.values || initial;
  const version = draft?.version ?? state.config_version;
  async function save(event) {
    event.preventDefault();
    const result = await act(async () => {
      const parsed = { ...values };
      for (const field of strategy.fields || []) if (field.type === 'json' && typeof parsed[field.name] === 'string') parsed[field.name] = JSON.parse(parsed[field.name]);
      return api('strategy', { strategy: parsed, version });
    });
    if (result) setDraft(null);
  }
  return <form className="strategy-parameters" onSubmit={save}><h3>手动调仓与单策略计划</h3><p className="small muted">保存后使用此策略及参数。已有运行实例仍使用各自的参数。</p><div className="form-grid"><ModuleFields fields={strategy.fields || []} values={values} disabled={busy} onChange={(name, value) => setDraft({ values: { ...values, [name]: value }, version })}/></div><div className="form-footer"><span className="small muted">总仓位上限 {percent(state.limits.max_gross)} · 单股上限 {percent(state.limits.max_weight)}</span><div className="button-row">{draft && <button className="button" type="button" disabled={busy} onClick={() => setDraft(null)}>还原参数</button>}<button className="button primary" disabled={busy || !strategy.enabled}>保存策略配置</button></div></div></form>;
}

function StrategyDeployments({ strategy, instances, state, busy, navigate }) {
  return <section>
    <div className="card-heading"><div><h3>运行此策略</h3><p>每个实例绑定自己的账户、参数和计划，环境以绑定账户为准。</p></div>
      <button className="button primary" disabled={busy || !strategy.enabled || state.strategy_manager?.corrupt} onClick={() => navigate({ page: 'runs', view: 'instances', create: true, strategy: strategy.type })}>创建运行实例</button></div>
    {!instances.length ? <p className="small muted">此策略还没有运行实例。可以先查看回测实验，再选择账户和资金比例。</p> : <div className="table-wrap"><table><thead><tr><th>实例</th><th>账户</th><th>环境</th><th>资金比例</th><th>状态</th><th>操作</th></tr></thead><tbody>
      {instances.map(item => { const broker = state.brokers.find(b => b.id === item.broker_id); return <tr key={item.id}><td>{item.name}</td><td>{broker?.name || item.broker_id}</td><td>{broker?.mode_label || '环境待核验'}</td><td>{percent(item.budget)}</td><td>{instanceStatus(item).label}</td><td><button className="text-link" onClick={() => navigate({ page: 'runs', view: 'instances', instance: item.id })}>查看详情</button></td></tr>; })}
    </tbody></table></div>}
  </section>;
}
