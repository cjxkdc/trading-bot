import React, { useEffect, useState } from 'react';
import { api } from '../api';
import { Dialog } from './Dialog';
import { time } from '../format';
import { automationAccountMatches, automationControls, automationOrderCheck, automationSchedule, automationView, scheduleChanged, scheduleSummary } from '../automation';


export function AutomationCard({ state, online, runtimeRunning, pending, busy, act, chooseBroker, navigate, compact = false }) {
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
    resolveRemainderReady: state.actions?.['resolve-remainder']?.enabled, closeTerminalReady: state.actions?.['close-terminal']?.enabled,
    closeUntransmittedReady: state.actions?.['close-untransmitted']?.enabled });
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
  async function closeUntransmitted() {
    if (!controls.canCloseUntransmitted || !accountMatches || !checked) return;
    const result = await act(() => api('jobs', { action: 'close-untransmitted', confirmation: state.broker.id }));
    if (result) setConfirming(false);
  }
  // A plan with a bound account can become a run instance: same account, data
  // source, schedule and parameters, 100% budget, created stopped.
  const migratable = !!automation?.binding?.broker_id && !automation?.corrupt;
  async function migrate() {
    const result = await act(() => api('automation/migrate', {}));
    if (result) { setConfirming(false); if (navigate && result.id) navigate({ page: 'runs', view: 'instances', instance: result.id }); }
  }
  return <section className="card automation-card" aria-labelledby="automation-title">
    <div className="card-heading"><div><h2 id="automation-title">单策略计划</h2><p>{scheduleSummary(automation?.config)}</p></div><span className={'tag ' + view.tone}>{view.label}</span></div>
    {state.managed_account && <p className="inline-warning">当前所选账户由实例管理。下方保留已绑定的单策略计划，账户操作需使用计划绑定账户。</p>}<div className="automation-binding"><span className="tag blue">{state.brokers?.find(item => item.id === (binding.broker_id || state.broker?.id))?.mode_label || '环境待核验'}</span><span>{binding.strategy_name || '未选择策略'}</span><span aria-hidden="true">/</span><span>{binding.broker_name || '未选择账户'}</span><span aria-hidden="true">/</span><span>{binding.source_name || '未选择数据源'}</span></div>
    {view.enabled || view.status === 'paused' ? <p className="automation-message" role="status">{view.message}</p> : <p className="automation-message">{automation?.config?.initial_entry === 'scheduled' ? '开启后先检查，首次建仓与后续调仓都遵循已保存计划。' : '开启后先检查，在下个可用交易时段建仓，之后按计划调仓。'}</p>}
    {!accountMatches && <div className="inline-warning"><p>此计划绑定 {binding.broker_name || '其他账户'}。核对订单前，请先切换到绑定账户。{view.enabled ? '关闭计划仍可操作。' : '该计划当前未开启。'}</p>{automation?.binding?.broker_id && chooseBroker && <button className="button" disabled={busy} onClick={() => chooseBroker(automation.binding.broker_id)}>选择计划绑定账户</button>}</div>}<div className="automation-timing"><div><span>{view.enabled && automation?.startup?.status === 'checked' ? '首次建仓时间' : '下次调仓时间'} · 本地时间</span><strong>{view.enabled && automation?.next_run ? time(automation.next_run) : view.enabled ? '检查后显示' : '尚未开启'}</strong></div><div><span>{orderCheck?.title || '最近自动执行（历史）'}</span><strong>{time(orderCheck?.at || automation?.last_run?.started_at || automation?.last_run?.at || automation?.last_run?.time)}</strong>{(orderCheck?.message || automation?.last_run?.message) && <small>{orderCheck?.message || automation.last_run.message}</small>}</div></div>
    <form onSubmit={save}>
      <details className="automation-settings" open={compact ? undefined : true}><summary>调仓设置{dirty ? ' · 未保存' : ''}</summary>
      <div className="automation-schedule">
        <label>调仓周期<select value={draft.cadence} disabled={!controls.canEdit} onChange={event => setDraft({ ...draft, cadence: event.target.value })}><option value="daily">每日</option><option value="weekly">每周</option><option value="monthly">每月</option></select></label>
        {draft.cadence === 'monthly' && <label>每月从几号开始<input type="number" min="1" max="28" required value={draft.day} disabled={!controls.canEdit} onChange={event => setDraft({ ...draft, day: event.target.value })}/></label>}
        {draft.cadence === 'weekly' && <label>每周哪一天<select value={draft.weekday} disabled={!controls.canEdit} onChange={event => setDraft({ ...draft, weekday: Number(event.target.value) })}>{['周一','周二','周三','周四','周五','周六','周日'].map((day, i) => <option key={day} value={i}>{day}</option>)}</select></label>}
        <label>计划时区<input required value={draft.timezone} placeholder="IANA 时区" disabled={!controls.canEdit} onChange={event => setDraft({ ...draft, timezone: event.target.value })}/></label>
        <label>每日更新数据<input type="time" required value={draft.prepare_time} disabled={!controls.canEdit} onChange={event => setDraft({ ...draft, prepare_time: event.target.value })}/></label>
        <label>调仓时间<input type="time" required value={draft.trade_time} disabled={!controls.canEdit} onChange={event => setDraft({ ...draft, trade_time: event.target.value })}/></label>
        <label>首次建仓<select value={draft.initial_entry} disabled={!controls.canEdit} onChange={event=>setDraft({...draft,initial_entry:event.target.value})}><option value="immediate">下个可用交易时段</option><option value="scheduled">等待计划调仓日</option></select></label>
      </div>
      <p className="small muted automation-schedule-note">以上时间按计划时区，交易窗口由账户的市场日历提供。回测导出使用同一份已保存计划。</p>
      {!view.enabled && <p className="small muted">更换周期后，请按相同周期重新回测。</p>}
      {view.enabled && <p className="small muted">关闭自动运行后可修改计划。</p>}
      {!view.enabled && <button className="button" type="submit" disabled={!controls.canEdit || !dirty}>保存计划</button>}
      </details>
      {view.blockers.length > 0 && <ul className="automation-blockers">{view.blockers.map((message, index) => <li key={index}>{message}</li>)}</ul>}
      <div className="form-footer"><span className="small muted">{dirty ? '计划有修改，请先保存。' : view.enabled ? '网页可以关闭，服务器会继续运行。' : '使用当前策略、数据源和绑定账户。'}</span><div className="button-row"><button className="button" type="button" disabled={busy || !accountMatches || !state.actions?.reconcile?.enabled} title={!accountMatches ? '先选择计划绑定账户，再核对订单。' : state.actions?.reconcile?.message} onClick={() => act(() => api('jobs', { action: 'reconcile' }))}>核对订单</button>{controls.canResolveRemainder && accountMatches && <button className="button" type="button" title={state.actions?.['resolve-remainder']?.message} onClick={() => setConfirming('resolve-remainder')}>结束未提交余单</button>}{controls.canCloseTerminal && accountMatches && orderCheck?.terminal > 0 && <button className="button danger" type="button" title={state.actions?.['close-terminal']?.message} onClick={() => setConfirming('close-terminal')}>关闭已撤单/拒单（{orderCheck.terminal}）</button>}{controls.canCloseUntransmitted && accountMatches && orderCheck?.untransmitted > 0 && <button className="button danger" type="button" title={state.actions?.['close-untransmitted']?.message} onClick={() => setConfirming('close-untransmitted')}>确认未发送并关闭（{orderCheck.untransmitted}）</button>}{migratable && <button className="button" type="button" disabled={busy} title="按同一账户、数据源、计划和当前参数创建一份停止状态的运行实例，资金比例 100%；旧计划关闭，历史保留。" onClick={() => setConfirming('migrate')}>迁移为运行实例</button>}{view.enabled ? <button className="button" type="button" disabled={!controls.canDisable} onClick={() => act(() => api('automation/disable', {}))}>关闭自动运行</button> : <button className="button primary" type="button" disabled={!controls.canEnable || state.managed_account} title={dirty ? '请先保存计划' : view.blockers.join('；')} onClick={() => setConfirming('enable')}>开启自动运行</button>}</div></div>
    </form>
    {(events.length > 0 || automation?.last_run) && <details className="automation-events"><summary>历史运行与通知</summary><p className="small muted">记录保留发生时的结果，当前状态见上方。</p>{automation?.last_run && <div className="automation-event"><time>{time(automation.last_run.started_at || automation.last_run.at || automation.last_run.time)}</time><span>最近自动执行：{automation.last_run.message || automation.last_run.status}</span></div>}<div>{events.slice(0, 12).map((event, index) => <div key={event.id || index} className={'automation-event ' + (['error', 'warning'].includes(event.severity) ? 'warning' : '')}><time>{time(event.at || event.time)}</time><span>{event.message}</span></div>)}</div></details>}
    {confirming && <Dialog labelledBy="automation-confirm-title" onDismiss={() => setConfirming(false)}><h2 id="automation-confirm-title">{confirming === 'resolve-remainder' ? '结束未提交的剩余计划' : confirming === 'close-terminal' ? '关闭已撤单/拒单' : confirming === 'close-untransmitted' ? '确认委托从未发送并关闭' : confirming === 'migrate' ? '迁移为运行实例' : '开启账户自动运行'}</h2>{confirming === 'migrate' ? <><p>用这个计划绑定的账户（{binding.broker_name || binding.broker_id}）、数据源（{binding.source_name || binding.source_id}）、调仓计划和「参数」页当前保存的策略参数，创建一份运行实例。</p><ul><li>资金比例设为 100%，仓位计算与旧计划一致；之后可在实例设置里调小。</li><li>实例创建后处于停止状态，要在运行详情里确认启动。</li><li>旧计划随即关闭，历史记录保留；已提交的委托不受影响。</li></ul><p className="small muted">计划仍有未核对批次，或模块、参数、代码与绑定时不同时，迁移会被拒绝。</p></> : confirming === 'close-untransmitted' ? <><p>程序会重新向 {selected.broker_name} 查询这些委托创建当日、次日和今天的订单列表；只有券商侧完全没有对应记录、本地账本也没有券商回执的委托，才会被标记为「已关闭」并写入审计记录（含查询结果）。</p><p className="small muted">这些委托当时可能因为 OpenD 未解锁、断线或网关拒绝而从未送达。关闭后账户可以重新执行新一轮；不会补单或撤单，也不代表组合达到目标仓位。若券商端能看到这些委托，请改用「关闭已撤单/拒单」或继续核对。</p><label className="review-check"><input type="checkbox" checked={checked} onChange={e => setChecked(e.target.checked)}/>我已在券商端（含历史订单）确认这些委托不存在。</label></> : confirming === 'close-terminal' ? <><p>重新向券商查询 {selected.broker_name} 的订单后，把券商确认已撤销或已拒绝的委托标记为「已关闭」，同批次未提交的计划一并结束，并写入审计记录。</p><p className="small muted">部分成交的数量按实际持仓保留，组合可能尚未达到目标；不会补单或撤单。存在状态不明、等待成交或部分成交的委托时会被拒绝。</p><label className="review-check"><input type="checkbox" checked={checked} onChange={e => setChecked(e.target.checked)}/>我已在券商端核对这些撤单或拒单的原因。</label></> : confirming === 'resolve-remainder' ? <><p>核对 {selected.broker_name} 的订单后，仅把从未提交到券商的剩余计划记为放弃。不会撤销券商订单，也不会提交新订单。</p><p className="small muted">已成交持仓会保留，因此组合可能尚未达到目标。若仍有部分成交、处理中或状态不明的订单，此操作会被阻止。结束余单不会开启自动运行。</p></> : <><p>使用 {selected.strategy_name}、{selected.source_name} 和 {selected.broker_name}。开启后立即检查，交易时段内开始首轮建仓；休市时等待下次开盘。后续按保存的周期自动调仓。</p><p className="small muted">切换模块或修改策略后需要重新确认计划；异常和未确认订单会暂停后续交易。</p></>}<div className="button-row"><button className="button" autoFocus onClick={() => setConfirming(false)}>取消</button>{confirming === 'migrate' ? <button className="button primary" disabled={busy} onClick={migrate}>确认迁移</button> : confirming === 'close-untransmitted' ? <button className="button primary" disabled={!controls.canCloseUntransmitted || !accountMatches || !checked} onClick={closeUntransmitted}>确认未发送并关闭</button> : confirming === 'close-terminal' ? <button className="button primary" disabled={!controls.canCloseTerminal || !accountMatches || !checked} onClick={closeTerminal}>确认关闭</button> : confirming === 'resolve-remainder' ? <button className="button primary" disabled={!controls.canResolveRemainder || !accountMatches} onClick={resolveRemainder}>确认结束未提交余单</button> : <button className="button primary" disabled={!controls.canEnable || state.managed_account} onClick={enable}>确认开启</button>}</div></Dialog>}
  </section>;
}
