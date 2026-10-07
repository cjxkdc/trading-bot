import React, { useEffect, useState } from 'react';
import { api } from '../api';
import { Dialog } from './Dialog';
import { time } from '../format';
import { automationAccountMatches, automationControls, automationOrderCheck, automationSchedule, automationView, scheduleChanged, scheduleSummary } from '../automation';


export function AutomationCard({ state, online, runtimeRunning, pending, busy, act, chooseBroker, compact = false }) {
  const automation = state.automation;
  const view = automationView(automation, { online, runtimeRunning });
  const savedConfig = JSON.stringify(automationSchedule(automation?.config));
  const [draft, setDraft] = useState(() => automationSchedule(automation?.config));
  const [confirming, setConfirming] = useState(false), [checked, setChecked] = useState(false);
  useEffect(() => { setChecked(false); }, [confirming]);
  useEffect(() => { setDraft(JSON.parse(savedConfig)); }, [savedConfig]);
  useEffect(() => { setConfirming(false); }, [state.strategy_name, state.broker?.id, state.source]);
  if (state.managed_account && !automation?.enabled && automation?.status !== 'paused') return <section className="empty"><strong>此账户由策略实例管理</strong><p>请在策略实例中管理运行，或选择未由实例管理的账户使用单策略计划。</p></section>;
  const dirty = scheduleChanged(draft, automation?.config);
  const controls = automationControls(automation, { online, runtimeRunning, pending, busy, dirty,
    brokerVerified: state.broker_verified === true, reconcileReady: state.actions?.reconcile?.enabled,
    resolveRemainderReady: state.actions?.['resolve-remainder']?.enabled, closeTerminalReady: state.actions?.['close-terminal']?.enabled });
  const accountMatches = automationAccountMatches(automation, state.broker?.id);
  const selected = { strategy_name: state.strategy_name, broker_name: state.broker?.name,
    source_name: state.sources?.find(item => item.id === state.source)?.name };
  const binding = (view.enabled || view.status === 'paused') && automation?.binding ? automation.binding : selected;
  const events = automation?.events || [];
  const orderCheck = automationOrderCheck(automation, state.jobs, state.broker?.id);
  async function save(event) {
    event.preventDefault();
    await act(() => api('automation/config', { ...draft, day: Number(draft.day), weekday: Number(draft.weekday) }), '计划已保存。');
    setConfirming(false);
  }
  async function enable() {
    if (!controls.canEnable || state.managed_account) return;
    const result = await act(() => api('automation/enable', {}));
    if (result) setConfirming(false);
  }
  async function resolveRemainder() {
    if (!controls.canResolveRemainder || !accountMatches) return;
    const result = await act(() => api('jobs', { action: 'resolve-remainder', confirmation: state.broker.id }));
    if (result) setConfirming(false);
  }
  async function closeTerminal() {
    if (!controls.canCloseTerminal || !accountMatches || !checked) return;
    const result = await act(() => api('jobs', { action: 'close-terminal', confirmation: state.broker.id }));
    if (result) setConfirming(false);
  }
  return <section className="card automation-card" aria-labelledby="automation-title">
    <div className="card-heading"><div><h2 id="automation-title">单策略计划</h2><p>{scheduleSummary(automation?.config)}</p></div><span className={'tag ' + view.tone}>{view.label}</span></div>
    {state.managed_account && <p className="inline-warning">当前所选账户由实例管理。下方保留已绑定的单策略计划，账户操作需使用计划绑定账户。</p>}<div className="automation-binding"><span className="tag blue">仅模拟盘</span><span>{binding.strategy_name || '未选择策略'}</span><span aria-hidden="true">/</span><span>{binding.broker_name || '未选择账户'}</span><span aria-hidden="true">/</span><span>{binding.source_name || '未选择数据源'}</span></div>
    {view.enabled || view.status === 'paused' ? <p className="automation-message" role="status">{view.message}</p> : <p className="automation-message">{automation?.config?.initial_entry === 'scheduled' ? '开启后先检查，首次建仓与后续调仓都遵循已保存计划。' : '开启后先检查，在下个可用交易时段建仓，之后按计划调仓。'}</p>}
    {!accountMatches && <div className="inline-warning"><p>此计划绑定 {binding.broker_name || '其他账户'}。核对订单前，请先切换到绑定账户。{view.enabled ? '关闭计划仍可操作。' : '该计划当前未开启。'}</p>{automation?.binding?.broker_id && chooseBroker && <button className="button" disabled={busy} onClick={() => chooseBroker(automation.binding.broker_id)}>选择计划绑定账户</button>}</div>}<div className="automation-timing"><div><span>{view.enabled && automation?.startup?.status === 'checked' ? '首次建仓时间' : '下次调仓时间'} · 本地时间</span><strong>{view.enabled && automation?.next_run ? time(automation.next_run) : view.enabled ? '检查后显示' : '尚未开启'}</strong></div><div><span>{orderCheck?.title || '最近自动执行（历史）'}</span><strong>{time(orderCheck?.at || automation?.last_run?.started_at || automation?.last_run?.at || automation?.last_run?.time)}</strong>{(orderCheck?.message || automation?.last_run?.message) && <small>{orderCheck?.message || automation.last_run.message}</small>}</div></div>
    <form onSubmit={save}>
      <details className="automation-settings" open={compact ? undefined : true}><summary>调仓设置{dirty ? ' · 未保存' : ''}</summary>
      <div className="automation-schedule">
        <label>调仓周期<select value={draft.cadence} disabled={!controls.canEdit} onChange={event => setDraft({ ...draft, cadence: event.target.value })}><option value="daily">每日</option><option value="weekly">每周</option><option value="monthly">每月</option></select></label>
        {draft.cadence === 'monthly' && <label>每月从几号开始<input type="number" min="1" max="28" required value={draft.day} disabled={!controls.canEdit} onChange={event => setDraft({ ...draft, day: event.target.value })}/></label>}
        {draft.cadence === 'weekly' && <label>每周哪一天<select value={draft.weekday} disabled={!controls.canEdit} onChange={event => setDraft({ ...draft, weekday: Number(event.target.value) })}>{['周一','周二','周三','周四','周五'].map((day, i) => <option key={day} value={i}>{day}</option>)}</select></label>}
        <label>每日更新数据<input type="time" required value={draft.prepare_time} disabled={!controls.canEdit} onChange={event => setDraft({ ...draft, prepare_time: event.target.value })}/></label>
        <label>调仓时间<input type="time" required value={draft.trade_time} disabled={!controls.canEdit} onChange={event => setDraft({ ...draft, trade_time: event.target.value })}/></label>
        <label>首次建仓<select value={draft.initial_entry} disabled={!controls.canEdit} onChange={event=>setDraft({...draft,initial_entry:event.target.value})}><option value="immediate">下个可用交易时段</option><option value="scheduled">等待计划调仓日</option></select></label>
      </div>
      <p className="small muted automation-schedule-note">以上为美东时间，自动处理夏令时；休市顺延。回测导出使用同一份已保存计划。</p>
      {!view.enabled && <p className="small muted">更换周期后，请按相同周期重新回测。</p>}
      {view.enabled && <p className="small muted">关闭自动运行后可修改计划。</p>}
      {!view.enabled && <button className="button" type="submit" disabled={!controls.canEdit || !dirty}>保存计划</button>}
      </details>
      {view.blockers.length > 0 && <ul className="automation-blockers">{view.blockers.map((message, index) => <li key={index}>{message}</li>)}</ul>}
      <div className="form-footer"><span className="small muted">{dirty ? '计划有修改，请先保存。' : view.enabled ? '网页可以关闭，服务器会继续运行。' : '使用当前策略、数据源和模拟账户。'}</span><div className="button-row"><button className="button" type="button" disabled={busy || !accountMatches || !state.actions?.reconcile?.enabled} title={!accountMatches ? '先选择计划绑定账户，再核对订单。' : state.actions?.reconcile?.message} onClick={() => act(() => api('jobs', { action: 'reconcile' }))}>核对订单</button>{controls.canResolveRemainder && accountMatches && <button className="button" type="button" title={state.actions?.['resolve-remainder']?.message} onClick={() => setConfirming('resolve-remainder')}>结束未提交余单</button>}{controls.canCloseTerminal && accountMatches && orderCheck?.terminal > 0 && <button className="button danger" type="button" title={state.actions?.['close-terminal']?.message} onClick={() => setConfirming('close-terminal')}>关闭已撤单/拒单（{orderCheck.terminal}）</button>}{view.enabled ? <button className="button" type="button" disabled={!controls.canDisable} onClick={() => act(() => api('automation/disable', {}))}>关闭自动运行</button> : <button className="button primary" type="button" disabled={!controls.canEnable || state.managed_account} title={dirty ? '请先保存计划' : view.blockers.join('；')} onClick={() => setConfirming('enable')}>开启自动运行</button>}</div></div>
    </form>
    {(events.length > 0 || automation?.last_run) && <details className="automation-events"><summary>历史运行与通知</summary><p className="small muted">记录保留发生时的结果，当前状态见上方。</p>{automation?.last_run && <div className="automation-event"><time>{time(automation.last_run.started_at || automation.last_run.at || automation.last_run.time)}</time><span>最近自动执行：{automation.last_run.message || automation.last_run.status}</span></div>}<div>{events.slice(0, 12).map((event, index) => <div key={event.id || index} className={'automation-event ' + (['error', 'warning'].includes(event.severity) ? 'warning' : '')}><time>{time(event.at || event.time)}</time><span>{event.message}</span></div>)}</div></details>}
    {confirming && <Dialog labelledBy="automation-confirm-title" onDismiss={() => setConfirming(false)}><h2 id="automation-confirm-title">{confirming === 'resolve-remainder' ? '结束未提交的剩余计划' : confirming === 'close-terminal' ? '关闭已撤单/拒单' : '开启模拟账户自动运行'}</h2>{confirming === 'close-terminal' ? <><p>重新向券商查询 {selected.broker_name} 的订单后，把券商确认已撤销或已拒绝的委托标记为「已关闭」，同批次未提交的计划一并结束，并写入审计记录。</p><p className="small muted">部分成交的股数按实际持仓保留，组合可能尚未达到目标；不会补单或撤单。存在状态不明、等待成交或部分成交的委托时会被拒绝。</p><label className="review-check"><input type="checkbox" checked={checked} onChange={e => setChecked(e.target.checked)}/>我已在券商端核对这些撤单或拒单的原因。</label></> : confirming === 'resolve-remainder' ? <><p>核对 {selected.broker_name} 的订单后，仅把从未提交到券商的剩余计划记为放弃。不会撤销券商订单，也不会提交新订单。</p><p className="small muted">已成交持仓会保留，因此组合可能尚未达到目标。若仍有部分成交、处理中或状态不明的订单，此操作会被阻止。结束余单不会开启自动运行。</p></> : <><p>使用 {selected.strategy_name}、{selected.source_name} 和 {selected.broker_name}。开启后立即检查，交易时段内开始首轮建仓；休市时等待下次开盘。后续按保存的周期自动调仓。</p><p className="small muted">切换模块或修改策略后需要重新确认计划；异常和未确认订单会暂停后续交易。</p></>}<div className="button-row"><button className="button" autoFocus onClick={() => setConfirming(false)}>取消</button>{confirming === 'close-terminal' ? <button className="button primary" disabled={!controls.canCloseTerminal || !accountMatches || !checked} onClick={closeTerminal}>确认关闭</button> : confirming === 'resolve-remainder' ? <button className="button primary" disabled={!controls.canResolveRemainder || !accountMatches} onClick={resolveRemainder}>确认结束未提交余单</button> : <button className="button primary" disabled={!controls.canEnable || state.managed_account} onClick={enable}>确认开启</button>}</div></Dialog>}
  </section>;
}
