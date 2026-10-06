import React, { useState } from 'react';
import { api, downloadCode } from '../api';
import { scheduleSummary } from '../automation';
import { budgetSummary, instanceDraft, instancePayload, instanceStatus, runnableBroker, latestInstancePreview } from '../strategy_instances';
import '../strategy_runs.css';
import { ModuleFields } from './ModuleFields';
import { StrategyReadme } from './StrategyReadme';

const time = value => value ? new Date(value).toLocaleString('zh-CN', { hour12: false }) : '—';
const percent = value => `${(Number(value) * 100).toFixed(1)}%`;

function InstanceForm({ state, item, busy, act, close }) {
  const [draft, setDraft] = useState(() => instanceDraft(state, item));
  const selected = state.strategies.find(x => x.type === draft.strategy.type);
  const schedule = draft.schedule;
  const setSchedule = (key, value) => setDraft({ ...draft, schedule: { ...schedule, [key]: value } });
  async function save(event) {
    event.preventDefault();
    const result = await act(() => api(item ? `strategy-instances/${item.id}` : 'strategy-instances', instancePayload(draft, selected.fields)));
    if (result) close();
  }
  return <section className="card instance-form"><div className="card-heading"><h2>{item ? '编辑实例' : '创建策略实例'}</h2><button className="button" disabled={busy} onClick={close}>收起</button></div>
    <form onSubmit={save}><div className="form-grid">
      <label>实例名称<input required maxLength="80" value={draft.name} disabled={busy} onChange={e => setDraft({ ...draft, name: e.target.value })}/></label>
      <label>策略<select value={draft.strategy.type} disabled={busy} onChange={e => { const strategy = state.strategies.find(x => x.type === e.target.value); setDraft({ ...draft, strategy: { type: strategy.type, ...strategy.defaults } }); }}>{state.strategies.filter(x => x.enabled).map(x => <option key={x.type} value={x.type}>{x.name}</option>)}</select></label>
      <label>模拟账户<select value={draft.broker_id} disabled={busy} onChange={e => setDraft({ ...draft, broker_id: e.target.value })}>{state.brokers.filter(runnableBroker).map(x => <option key={x.id} value={x.id}>{x.name}</option>)}</select></label>
      <label>数据源<select value={draft.source_id} disabled={busy} onChange={e => setDraft({ ...draft, source_id: e.target.value })}>{state.sources.filter(x => x.enabled && x.capabilities.includes('collect')).map(x => <option key={x.id} value={x.id}>{x.name}</option>)}</select></label>
      <label>账户资金比例 (%)<input type="number" min="0.01" max="100" step="0.01" required value={draft.budget} disabled={busy} onChange={e => setDraft({ ...draft, budget: e.target.value })}/></label>
    </div><p className="small muted">比例按账户净值分配。此账户运行或保留持仓的实例已占用 {percent(budgetSummary(state.strategy_manager.instances.filter(x => x.id !== item?.id), draft.broker_id))}。</p>
    <details open className="details"><summary>运行计划</summary><div className="form-grid">
      <label>调仓周期<select value={schedule.cadence} disabled={busy} onChange={e => setSchedule('cadence', e.target.value)}><option value="daily">每日</option><option value="weekly">每周</option><option value="monthly">每月</option></select></label>
      {schedule.cadence === 'weekly' && <label>调仓日<select value={schedule.weekday} disabled={busy} onChange={e => setSchedule('weekday', Number(e.target.value))}>{['周一','周二','周三','周四','周五'].map((x, i) => <option key={i} value={i}>{x}</option>)}</select></label>}
      {schedule.cadence === 'monthly' && <label>每月从几号开始<input type="number" min="1" max="28" required value={schedule.day} disabled={busy} onChange={e => setSchedule('day', Number(e.target.value))}/></label>}
      <label>每日更新数据 · 美东时间<input type="time" required value={schedule.prepare_time} disabled={busy} onChange={e => setSchedule('prepare_time', e.target.value)}/></label>
      <label>执行开始 · 美东时间<input type="time" required value={schedule.trade_time} disabled={busy} onChange={e => setSchedule('trade_time', e.target.value)}/></label>
      <label>执行窗口 (分钟)<input type="number" min="5" max="120" required value={schedule.window_minutes} disabled={busy} onChange={e => setSchedule('window_minutes', Number(e.target.value))}/></label>
      <label>首次建仓<select value={schedule.initial_entry} disabled={busy} onChange={e => setSchedule('initial_entry', e.target.value)}><option value="immediate">下个可用交易时段</option><option value="scheduled">等待计划调仓日</option></select></label>
    </div><p className="small muted">自动处理夏令时与休市顺延；窗口外等待，不补发上一窗口订单。</p></details>
    <details className="details"><summary>策略参数</summary><div className="form-grid"><ModuleFields fields={selected?.fields || []} values={draft.strategy} disabled={busy} onChange={(key, value) => setDraft({ ...draft, strategy: { ...draft.strategy, [key]: value } })}/></div></details>
    <StrategyReadme content={selected?.readme?.content} filename={selected?.readme?.path} issue={selected?.readme_issue}/>
    <div className="form-footer"><span className="small muted">保存后使用各自参数运行，不修改策略库的当前配置。</span><button className="button primary" disabled={busy}>保存实例</button></div></form>
  </section>;
}

function InstanceDetails({ state, item, busy, act }) {
  const [logs, setLogs] = useState(null), [exporting, setExporting] = useState(false);
  const [range, setRange] = useState({ start: state.backtest_config?.start || '2020-01-01', end: state.backtest_config?.end || '2025-12-31', slippage_bps: state.backtest_config?.slippage_bps ?? 10 });
  const strategy = state.strategies.find(x => x.type === item.strategy.type);
  const preview = latestInstancePreview(state.jobs, item);
  async function exportCode(event) {
    event.preventDefault();
    await act(async () => { const result = await api(`strategy-instances/${item.id}/export`, range); await downloadCode(result.id); return result; });
  }
  return <div className="instance-details"><div className="instance-facts"><div><span>最近启动</span><strong>{time(item.started_at)}</strong></div><div><span>最近停止</span><strong>{time(item.stopped_at)}</strong></div><div><span>最近执行</span><strong>{time(item.last_run?.at)}</strong></div></div>
    {state.active?.action === 'strategy-preview' && state.active.instance_id === item.id && <p className="small muted" role="status">正在用已有数据预览选股…</p>}
    {preview && <section className="selection-preview"><div className="card-heading"><h3>最近一次选股预览</h3><span className="tag muted">研究预览</span></div><p className="small muted">数据截至 {preview.data_as_of || '—'} · {preview.candidate_count} 个候选 · 预览于 {time(preview.created_at)}</p>
      {preview.targets.length ? <div className="table-wrap"><table><thead><tr><th>股票</th><th>得分</th><th>策略内比例</th><th>账户目标比例</th></tr></thead><tbody>{preview.targets.map(x => <tr key={x.ticker}><td>{x.ticker}</td><td>{Number(x.score).toFixed(3)}</td><td>{percent(x.weight)}</td><td>{percent(x.account_weight)}</td></tr>)}</tbody></table></div> : <p className="small muted">当前数据下没有选中股票。</p>}
      <p className="small muted">本实例目标股票仓位 {percent(preview.account_target_gross)}。只计算选股和比例，未读取券商报价；整股数量与订单在运行时重新计算。</p></section>}
    <details className="details"><summary>参数与策略说明</summary><pre className="instance-json">{JSON.stringify(item.strategy, null, 2)}</pre><StrategyReadme content={strategy?.readme?.content} filename={strategy?.readme?.path} issue={strategy?.readme_issue}/></details>
    <details className="details"><summary>持仓归属 · {item.positions.length} 只</summary>{item.positions.length ? <div className="table-wrap"><table><thead><tr><th>股票</th><th>内部归属股数</th></tr></thead><tbody>{item.positions.map(x => <tr key={x.ticker}><td>{x.ticker}</td><td>{x.quantity}</td></tr>)}</tbody></table></div> : <p className="small muted">尚无已确认的实例持仓。</p>}<p className="small muted">同一股票可属于多份实例；券商展示合并持仓，内部归属在确认成交后更新。</p></details>
    <details className="details"><summary>运行日志</summary>{item.events.map((event, i) => <div className="log-line" key={i}><time>{time(event.at)}</time><span>{event.message}</span></div>)}<button className="button" disabled={busy} onClick={() => act(async () => { const result = await api(`strategy-instances/${item.id}/logs`); setLogs(result.items); return null; })}>读取执行记录</button>{logs?.map(log => <details className="details" key={log.cycle_id}><summary>{time(log.created_at)} · {log.message}</summary><pre className="instance-json">{JSON.stringify(log, null, 2)}</pre></details>)}</details>
    <button className="text-link" onClick={() => setExporting(!exporting)}>按此实例导出回测</button>{exporting && <form className="instance-export" onSubmit={exportCode}><label>开始日期<input type="date" required value={range.start} onChange={e => setRange({ ...range, start: e.target.value })}/></label><label>结束日期<input type="date" required value={range.end} onChange={e => setRange({ ...range, end: e.target.value })}/></label><label>滑点 (bps)<input type="number" min="0" max="9999" value={range.slippage_bps} onChange={e => setRange({ ...range, slippage_bps: Number(e.target.value) })}/></label><button className="button" disabled={busy}>导出 QuantConnect 代码</button><p className="small muted">单独回测此实例的策略和计划；多策略账户的合并表现从实际运行记录观察。</p></form>}
  </div>;
}

export function StrategyRuns({ state, busy, pending, act }) {
  const manager = state.strategy_manager || { instances: [], running_count: 0, limits: {} };
  const [editing, setEditing] = useState(null), [expanded, setExpanded] = useState(null), [confirm, setConfirm] = useState(null);
  const [limits, setLimits] = useState(manager.limits);
  const items = manager.instances;
  async function confirmed() {
    const { item, action } = confirm;
    const result = await act(() => api(`strategy-instances/${item.id}/${action}`, action === 'remove' ? {} : { confirmation: item.id }));
    if (result) setConfirm(null);
  }
  return <>
    <div className="section-toolbar"><div><h2>策略运行</h2><p className="small muted">{manager.running_count} 份已开启 · {items.length} 份实例</p></div><button className="button primary" disabled={busy || manager.corrupt} onClick={() => setEditing({})}>创建实例</button></div>
    {manager.corrupt && <div className="alert error">运行状态无法核对，实例已停用；原文件保留供检查。</div>}
    {editing && <InstanceForm key={editing.id || 'new'} state={state} item={editing.id ? editing : null} busy={busy} act={act} close={() => setEditing(null)}/>}
    {!items.length && !editing && <section className="card instance-empty"><h2>创建第一份策略实例</h2><p>选一套策略，设置账户资金比例与调仓计划，后台会按计划运行。</p><p className="small muted">可以用同一策略创建不同参数的实例；共用账户时先合并目标，再提交一批净订单。</p></section>}
    <div className="instance-accounts">{manager.accounts?.map(account => <section className="card account-budget" key={account.broker_id}><div className="card-heading"><h3>{state.brokers.find(x => x.id === account.broker_id)?.name || account.broker_id}</h3>{account.pending && <span className="tag amber">有待核对批次</span>}</div>
      <div className="budget-facts"><div><span>已预留资金比例</span><strong>{percent(account.reserved_budget)}</strong></div><div><span>可分配比例</span><strong>{percent(account.available_budget)}</strong></div></div>
      <div className="budget-meter" role="progressbar" aria-label="已预留账户资金" aria-valuenow={Number(account.reserved_budget) * 100} aria-valuemin="0" aria-valuemax="100"><span style={{ width: `${Math.min(100, Number(account.reserved_budget) * 100)}%` }}/></div>
      <p className="small muted">{account.running_count} 份运行 · {account.stopped_holding_count} 份停止并保留持仓。无持仓的停止实例不预留资金。</p></section>)}</div>
    <div className="instance-list">{items.map(item => { const view = instanceStatus(item); return <article className="card instance-card" key={item.id}>
      <div className="card-heading"><div><h2>{item.name}</h2><p>{state.strategies.find(s => s.type === item.strategy.type)?.name || item.strategy.type} · {state.brokers.find(b => b.id === item.broker_id)?.name || item.broker_id} · {state.sources.find(s => s.id === item.source_id)?.name || item.source_id}</p></div><span className={'tag ' + view.tone}>{view.label}</span></div>
      <div className="instance-facts"><div><span>资金比例</span><strong>{percent(item.budget)}</strong></div><div><span>运行计划</span><strong>{scheduleSummary(item.schedule)}</strong></div><div><span>下次执行 · 本地时间</span><strong>{time(item.next_run)}</strong></div></div>
      <p className={'small ' + (item.status === 'paused' ? 'inline-warning' : 'muted')} role="status">{item.message}</p>
      <div className="button-row instance-buttons">{item.enabled ? <button className="button" disabled={pending} onClick={() => act(() => api(`strategy-instances/${item.id}/stop`, {}))}>停止</button> : <button className="button primary" disabled={busy || item.pending || manager.corrupt} onClick={() => setConfirm({ item, action: 'start' })}>启动</button>}
        <button className="button" disabled={busy || item.pending} onClick={() => setConfirm({ item, action: 'once' })}>执行一轮</button>
        <button className="button" disabled={busy || item.pending || manager.corrupt} title={item.pending ? '先核对未完成批次' : '使用已有数据，休市时也可预览'} onClick={() => { setExpanded(item.id); act(() => api(`strategy-instances/${item.id}/preview`, {})); }}>预览选股</button>
        <button className="button" disabled={busy} onClick={() => act(() => api(`strategy-instances/${item.id}/check`, {}))}>检查连接</button>
        {item.pending && <button className="button" disabled={busy} onClick={() => act(() => api(`strategy-instances/${item.id}/reconcile`, {}))}>核对订单与归属</button>}
        {item.pending && !item.enabled && <button className="button" disabled={busy} onClick={() => setConfirm({ item, action: 'resolve' })}>结束未提交余单</button>}
        <button className="button" disabled={busy || item.enabled || item.pending} onClick={() => setEditing(item)}>设置</button>
        <button className="button" onClick={() => setExpanded(expanded === item.id ? null : item.id)}>{expanded === item.id ? '收起详情' : '详情与日志'}</button>
        {!item.enabled && !item.positions.length && <button className="text-link" disabled={busy || item.pending} onClick={() => setConfirm({ item, action: 'remove' })}>删除实例</button>}
      </div>{expanded === item.id && <InstanceDetails state={state} item={item} busy={busy} act={act}/>}
    </article>; })}</div>
    {Object.entries(manager.unmanaged || {}).some(([, pos]) => Object.keys(pos).length) && <details className="card details"><summary>账户中保留的其他持仓</summary>{Object.entries(manager.unmanaged).map(([broker, positions]) => <p key={broker} className="small muted">{state.brokers.find(b => b.id === broker)?.name || broker}：{Object.entries(positions).map(([symbol, q]) => `${symbol} ${q} 股`).join('，')}</p>)}</details>}
    <details className="card details"><summary>计算与日志限制</summary><form className="instance-export" onSubmit={event => { event.preventDefault(); act(() => api('strategy-instances/limits', limits)); }}><label>每次策略计算上限 (秒)<input type="number" min="10" max="120" value={limits.selection_timeout_seconds || 60} onChange={e => setLimits({ ...limits, selection_timeout_seconds: Number(e.target.value) })}/></label><label>保留最近执行日志 (轮)<input type="number" min="20" max="500" value={limits.log_retention_runs || 100} onChange={e => setLimits({ ...limits, log_retention_runs: Number(e.target.value) })}/></label><button className="button" disabled={busy || manager.running_count > 0 || items.some(x => x.pending)}>保存限制</button></form><p className="small muted">每份策略在独立计算进程中运行，输入上限 32 MB。Linux 服务器另有限制：768 MB 内存、60 秒 CPU 时间。交易进程等待券商确认，不会因计算超时而被强制终止。</p></details>
    {confirm && <div className="modal-backdrop" onClick={() => setConfirm(null)}><div className="modal" role="dialog" aria-modal="true" aria-labelledby="instance-confirm-title" onClick={e => e.stopPropagation()}><h2 id="instance-confirm-title">{confirm.action === 'remove' ? '删除策略实例' : confirm.action === 'resolve' ? '结束未提交余单' : confirm.action === 'once' ? '执行一轮模拟交易' : '启动策略实例'}</h2><p>{confirm.item.name} · 资金比例 {percent(confirm.item.budget)}。</p><p>{confirm.action === 'remove' ? '删除此实例，历史记录保留。' : confirm.action === 'resolve' ? '核对已提交订单后，结束此账户批次中尚未提交的委托，按实际成交更新持仓归属。保留已成交持仓；本操作不提交新订单，也不撤销已提交订单。' : confirm.action === 'once' ? '先检查账户与数据，进入可用交易窗口后执行一轮；原先停止的实例完成后继续停止。' : '先检查账户与数据，再按自己的计划进行模拟建仓和调仓。关闭网页后后台继续运行。'}</p><div className="button-row"><button className="button" autoFocus onClick={() => setConfirm(null)}>取消</button><button className="button primary" disabled={busy} onClick={confirmed}>确认</button></div></div></div>}
  </>;
}
