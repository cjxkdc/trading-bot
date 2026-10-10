import React, { useState } from 'react';
import { api, downloadCode } from '../api';
import { scheduleSummary } from '../automation';
import { budgetSummary, instanceDraft, instancePayload, instanceStatus, runnableBroker, latestInstancePreview } from '../strategy_instances';

import { ModuleFields } from './ModuleFields';
import { StrategyReadme } from './StrategyReadme';
import { percent, time } from '../format';


export function InstanceForm({ state, item, busy, act, close, initialStrategy }) {
  const [draft, setDraft] = useState(() => instanceDraft(state, item, initialStrategy));
  const selected = state.strategies.find(x => x.type === draft.strategy.type);
  const schedule = draft.schedule;
  const broker = state.brokers.find(x => x.id === draft.broker_id);
  const setSchedule = (key, value) => setDraft({ ...draft, schedule: { ...schedule, [key]: value } });
  async function save(event) {
    event.preventDefault();
    const result = await act(() => api(item ? `strategy-instances/${item.id}` : 'strategy-instances', instancePayload(draft, selected.fields)));
    if (result) close();
  }
  return <section className="card instance-form"><div className="card-heading"><div><h2>{item ? '修改运行设置' : '运行设置'}</h2><p>策略、账户、数据源、资金比例和计划。</p></div><button className="button" disabled={busy} onClick={close}>取消</button></div>
    <form onSubmit={save}><div className="form-grid">
      <label>运行名称<input required maxLength="80" value={draft.name} disabled={busy} onChange={e => setDraft({ ...draft, name: e.target.value })}/></label>
      <label>策略<select value={draft.strategy.type} disabled={busy} onChange={e => { const strategy = state.strategies.find(x => x.type === e.target.value); setDraft({ ...draft, strategy: { type: strategy.type, ...strategy.defaults } }); }}>{state.strategies.filter(x => x.enabled).map(x => <option key={x.type} value={x.type}>{x.name}</option>)}</select></label>
      <label>运行账户<select value={draft.broker_id} disabled={busy} onChange={e => { const next = state.brokers.find(x => x.id === e.target.value); setDraft({ ...draft, broker_id: e.target.value, schedule: { ...draft.schedule, ...next?.execution_context?.schedule_defaults, timezone: next?.execution_context?.timezone || '' } }); }}>{state.brokers.filter(runnableBroker).map(x => <option key={x.id} value={x.id}>{x.name} · {x.mode_label || '环境待核验'}</option>)}</select></label>
      <label>数据源<select value={draft.source_id} disabled={busy} onChange={e => setDraft({ ...draft, source_id: e.target.value })}>{state.sources.filter(x => x.enabled && x.capabilities.includes('collect')).map(x => <option key={x.id} value={x.id}>{x.name}</option>)}</select></label>
      <label>账户资金比例 (%)<input type="number" min="0.01" max="100" step="0.01" required value={draft.budget} disabled={busy} onChange={e => setDraft({ ...draft, budget: e.target.value })}/></label>
    </div><p className="small muted">比例按账户净值分配。此账户运行或保留持仓的实例已占用 {percent(budgetSummary(state.strategy_manager.instances.filter(x => x.id !== item?.id), draft.broker_id))}。</p>
    {broker?.execution_enabled === false && <p className="inline-warning">{broker.execution_reason || '此账户尚未开放执行；可以保存配置并检查连接。'}</p>}
    <details open className="details"><summary>运行计划</summary><div className="form-grid">
      <label>调仓周期<select value={schedule.cadence} disabled={busy} onChange={e => setSchedule('cadence', e.target.value)}><option value="daily">每日</option><option value="weekly">每周</option><option value="monthly">每月</option></select></label>
      {schedule.cadence === 'weekly' && <label>调仓日<select value={schedule.weekday} disabled={busy} onChange={e => setSchedule('weekday', Number(e.target.value))}>{['周一','周二','周三','周四','周五','周六','周日'].map((x, i) => <option key={i} value={i}>{x}</option>)}</select></label>}
      {schedule.cadence === 'monthly' && <label>每月从几号开始<input type="number" min="1" max="28" required value={schedule.day} disabled={busy} onChange={e => setSchedule('day', Number(e.target.value))}/></label>}
      <label>计划时区<input required value={schedule.timezone} placeholder="IANA 时区，如 Asia/Hong_Kong" disabled={busy} onChange={e => setSchedule('timezone', e.target.value)}/></label>
      <label>每日更新数据<input type="time" required value={schedule.prepare_time} disabled={busy} onChange={e => setSchedule('prepare_time', e.target.value)}/></label>
      <label>执行开始<input type="time" required value={schedule.trade_time} disabled={busy} onChange={e => setSchedule('trade_time', e.target.value)}/></label>
      <label>执行窗口 (分钟)<input type="number" min="5" max="120" required value={schedule.window_minutes} disabled={busy} onChange={e => setSchedule('window_minutes', Number(e.target.value))}/></label>
      <label>首次建仓<select value={schedule.initial_entry} disabled={busy} onChange={e => setSchedule('initial_entry', e.target.value)}><option value="immediate">下个可用交易时段</option><option value="scheduled">等待计划调仓日</option></select></label>
    </div><p className="small muted">自动处理夏令时与休市顺延；窗口外等待，不补发上一窗口订单。</p></details>
    <details className="details"><summary>策略参数</summary><div className="form-grid"><ModuleFields fields={selected?.fields || []} values={draft.strategy} disabled={busy} onChange={(key, value) => setDraft({ ...draft, strategy: { ...draft.strategy, [key]: value } })}/></div></details>
    <StrategyReadme content={selected?.readme?.content} filename={selected?.readme?.path} issue={selected?.readme_issue}/>
    <div className="form-footer"><span className="small muted">保存后使用各自参数运行，不修改策略库的当前配置。</span><button className="button primary" disabled={busy}>{item ? '保存修改' : '保存运行'}</button></div></form>
  </section>;
}
