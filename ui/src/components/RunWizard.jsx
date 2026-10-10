import React, { useEffect, useState } from 'react';
import { api } from '../api';
import { Button, Tag } from '../ui';
import { ModuleFields } from './ModuleFields';
import { scheduleSummary } from '../automation';
import { budgetSummary, instanceDraft, instancePayload, latestInstancePreview, runnableBroker, withTimeDefaults } from '../strategy_instances';
import { cls, percent, time } from '../format';

// "新建运行": six steps, one question each. Steps 1–4 only edit a draft; the
// instance is saved when leaving step 4, step 5 runs the read-only checks on it,
// and step 6 is the explicit confirmation to start (or to run once by hand).
export const WIZARD_STEPS = [['strategy', '策略'], ['account', '账户'], ['data', '数据'], ['plan', '资金与计划'], ['check', '检查'], ['launch', '启动']];

export function RunWizard({ state, busy, pending, act, initialStrategy, navigate }) {
  const [draft, setDraft] = useState(() => {
    const initial = instanceDraft(state, null, initialStrategy);
    const broker = state.brokers.find(x => x.id === initial.broker_id);
    return { ...initial, name: initial.name.replace(/新实例$/, '新运行'), schedule: withTimeDefaults(initial.schedule, broker) };
  });
  const [step, setStep] = useState(0);
  const [mode, setMode] = useState('scheduled');
  const [instanceId, setInstanceId] = useState(null);
  const [check, setCheck] = useState({ phase: 'idle', checkJobId: null, previewJobId: null, previewRequested: false });
  const [error, setError] = useState('');
  const strategy = state.strategies.find(x => x.type === draft.strategy.type);
  const broker = state.brokers.find(x => x.id === draft.broker_id);
  const source = state.sources.find(x => x.id === draft.source_id);
  const item = instanceId ? state.strategy_manager?.instances?.find(x => x.id === instanceId) : null;
  const checkJob = state.jobs?.find(job => job.id === check.checkJobId && job.finished_at) || null;
  const previewJob = state.jobs?.find(job => job.id === check.previewJobId && job.finished_at) || null;
  const preview = item ? latestInstancePreview(state.jobs, item) : null;
  const checksDone = check.phase === 'failed' || (check.phase === 'previewing' && !!previewJob);
  const idle = !pending && !state.active;

  useEffect(() => {
    // After the connection check finishes, run the selection preview once.
    if (step !== 4 || !instanceId || check.phase !== 'checking' || !checkJob || check.previewRequested || !idle) return;
    setCheck(current => ({ ...current, previewRequested: true }));
    act(() => api(`strategy-instances/${instanceId}/preview`, {})).then(result => {
      setCheck(current => ({ ...current, phase: result?.job_id ? 'previewing' : 'failed', previewJobId: result?.job_id || null }));
    });
  }, [step, instanceId, check.phase, check.previewRequested, checkJob, idle, act]);

  function choose(values) { setError(''); setDraft({ ...draft, ...values }); }
  function chooseBroker(id) {
    const next = state.brokers.find(x => x.id === id);
    choose({ broker_id: id, schedule: withTimeDefaults({ ...draft.schedule, ...next?.execution_context?.schedule_defaults, timezone: next?.execution_context?.timezone || draft.schedule.timezone }, next) });
  }
  function setSchedule(key, value) { choose({ schedule: { ...draft.schedule, [key]: value } }); }

  async function runChecks(id) {
    setCheck({ phase: 'checking', checkJobId: null, previewJobId: null, previewRequested: false });
    const started = await act(() => api(`strategy-instances/${id}/check`, {}));
    setCheck(current => ({ ...current, phase: started?.job_id ? 'checking' : 'failed', checkJobId: started?.job_id || null }));
  }

  async function next() {
    setError('');
    if (step === 0 && !strategy?.enabled) return setError('请选择一个可用的策略。');
    if (step === 1 && !runnableBroker(broker)) return setError('请选择一个可以自动运行的账户。');
    if (step === 2 && !source) return setError('请选择一个数据源。');
    if (step === 3) {
      if (!draft.schedule.timezone) return setError('请填写计划时区，例如 America/New_York。');
      if (!draft.schedule.prepare_time || !draft.schedule.trade_time) return setError('请填写每日更新数据和执行开始的时间。');
      let payload;
      try { payload = instancePayload({ ...draft, name: draft.name.trim() }, strategy?.fields); } catch (failure) { return setError(failure.message); }
      if (!payload.name) return setError('请给这个运行起个名字。');
      const result = await act(() => api('strategy-instances', payload));
      if (!result?.id) return;
      setInstanceId(result.id);
      setStep(4);
      runChecks(result.id);
      return;
    }
    setStep(step + 1);
  }

  async function launch(action) {
    const result = await act(() => api(`strategy-instances/${instanceId}/${action}`, { confirmation: instanceId }));
    if (result) navigate({ page: 'runs', view: 'instances', instance: instanceId });
  }

  const stepper = <ol className="wizard-steps" aria-label="新建运行的步骤">
    {WIZARD_STEPS.map(([id, label], index) => <React.Fragment key={id}>
      {index > 0 && <li className="wizard-line" aria-hidden="true"/>}
      <li className={cls('wizard-step', index < step && 'done', index === step && 'current')} aria-current={index === step ? 'step' : undefined}>
        <i aria-hidden="true">{index < step ? '✓' : index + 1}</i><span>{label}</span>
      </li>
    </React.Fragment>)}
  </ol>;

  const content = [
    () => <>
      <h2>第 1 步 · 选择策略</h2><p className="lead">策略决定买什么、买多少比例。账户、资金和计划在后面几步选择。</p>
      <div className="option-grid" role="radiogroup" aria-label="策略">
        {state.strategies.map(entry => <label key={entry.type} className={cls('option-card', draft.strategy.type === entry.type && 'selected', !entry.enabled && 'disabled')}>
          <input type="radio" name="wizard-strategy" value={entry.type} checked={draft.strategy.type === entry.type} disabled={!entry.enabled || busy} onChange={() => choose({ strategy: { type: entry.type, ...entry.defaults }, name: `${entry.name} · 新运行` })}/>
          <span className="option-body"><span className="option-title"><strong>{entry.name}</strong><span className="small muted">v{entry.version}</span></span><small>{entry.enabled ? entry.type : entry.reason || '暂不可用'}</small></span>
        </label>)}
      </div>
      {strategy?.fields?.length > 0 && <details className="details"><summary>调整策略参数（可选）</summary><div className="form-grid"><ModuleFields fields={strategy.fields} values={draft.strategy} disabled={busy} onChange={(key, value) => choose({ strategy: { ...draft.strategy, [key]: value } })}/></div></details>}
      <p className="small muted">想先看规则或改代码：<button type="button" className="text-link" onClick={() => navigate({ page: 'strategies', strategy: draft.strategy.type, tab: 'readme' })}>打开「策略」</button></p>
    </>,
    () => <>
      <h2>第 2 步 · 选择账户</h2><p className="lead">策略会在这个账户上运行。实盘账户需要单独授权；没连接的账户先去「连接」里检查。</p>
      <div className="option-grid" role="radiogroup" aria-label="账户">
        {state.brokers.map(entry => { const runnable = runnableBroker(entry); return <label key={entry.id} className={cls('option-card', draft.broker_id === entry.id && 'selected', !runnable && 'disabled')}>
          <input type="radio" name="wizard-broker" value={entry.id} checked={draft.broker_id === entry.id} disabled={!runnable || busy} onChange={() => chooseBroker(entry.id)}/>
          <span className="option-body">
            <span className="option-title"><strong>{entry.name}</strong><Tag tone={entry.mode === 'live' ? 'red' : entry.mode === 'paper' ? 'muted' : 'blue'}>{entry.mode_label || '环境待核验'}</Tag></span>
            <small>{!entry.enabled ? entry.reason || '未启用' : !runnable ? '此账户模块不能用于自动运行' : entry.execution_enabled === false ? entry.execution_reason || '尚未开放执行，可以先保存配置' : [entry.execution_context?.market, entry.execution_context?.currency, entry.execution_context?.timezone].filter(Boolean).join(' · ') || '可用于自动运行'}</small>
          </span>
        </label>; })}
      </div>
      <p className="small muted">账户的登录、授权和连接检查在 <button type="button" className="text-link" onClick={() => navigate('broker')}>「连接」</button> 里完成。</p>
    </>,
    () => <>
      <h2>第 3 步 · 选择数据源</h2><p className="lead">运行时按策略声明的范围和字段自动采集；首次使用某个数据源，先在「连接」里配置并检查权限。</p>
      <div className="option-grid" role="radiogroup" aria-label="数据源">
        {state.sources.filter(entry => entry.capabilities?.includes('collect')).map(entry => <label key={entry.id} className={cls('option-card', draft.source_id === entry.id && 'selected', !entry.enabled && 'disabled')}>
          <input type="radio" name="wizard-source" value={entry.id} checked={draft.source_id === entry.id} disabled={!entry.enabled || busy} onChange={() => choose({ source_id: entry.id })}/>
          <span className="option-body"><span className="option-title"><strong>{entry.name}</strong>{entry.configured === false && <Tag tone="amber">未配置</Tag>}</span><small>{entry.enabled ? entry.message || entry.description || '' : entry.reason || '暂不可用'}</small></span>
        </label>)}
      </div>
      <p className="small muted">数据源的密钥和权限检查在 <button type="button" className="text-link" onClick={() => navigate('data')}>「连接 → 数据源」</button> 里完成。</p>
    </>,
    () => <>
      <h2>第 4 步 · 资金与计划</h2><p className="lead">资金比例是账户净值的上限，不换算成金额。计划按账户所在市场的交易日历执行。</p>
      <div className="form-grid">
        <label>运行名称<input required maxLength="80" value={draft.name} disabled={busy} onChange={event => choose({ name: event.target.value })}/></label>
        <label>账户资金比例 (%)<input type="number" min="0.01" max="100" step="0.01" required value={draft.budget} disabled={busy} onChange={event => choose({ budget: event.target.value })}/></label>
        <label>运行方式<select value={mode} disabled={busy} onChange={event => setMode(event.target.value)}><option value="scheduled">按计划自动运行</option><option value="manual">只在我点「执行一轮」时运行</option></select></label>
      </div>
      <p className="small muted">{broker?.name || '所选账户'} 上运行或保留持仓的其他运行已占用 {percent(budgetSummary(state.strategy_manager?.instances || [], draft.broker_id))}。</p>
      <div className="form-grid">
        <label>调仓周期<select value={draft.schedule.cadence} disabled={busy} onChange={event => setSchedule('cadence', event.target.value)}><option value="daily">每日</option><option value="weekly">每周</option><option value="monthly">每月</option></select></label>
        {draft.schedule.cadence === 'weekly' && <label>调仓日<select value={draft.schedule.weekday} disabled={busy} onChange={event => setSchedule('weekday', Number(event.target.value))}>{['周一', '周二', '周三', '周四', '周五', '周六', '周日'].map((day, index) => <option key={index} value={index}>{day}</option>)}</select></label>}
        {draft.schedule.cadence === 'monthly' && <label>每月从几号开始<input type="number" min="1" max="28" required value={draft.schedule.day} disabled={busy} onChange={event => setSchedule('day', Number(event.target.value))}/></label>}
        <label>计划时区<input required value={draft.schedule.timezone} placeholder="IANA 时区，如 America/New_York" disabled={busy} onChange={event => setSchedule('timezone', event.target.value)}/></label>
        <label>每日更新数据<input type="time" required value={draft.schedule.prepare_time} disabled={busy} onChange={event => setSchedule('prepare_time', event.target.value)}/></label>
        <label>执行开始<input type="time" required value={draft.schedule.trade_time} disabled={busy} onChange={event => setSchedule('trade_time', event.target.value)}/></label>
        <label>执行窗口 (分钟)<input type="number" min="5" max="120" required value={draft.schedule.window_minutes} disabled={busy} onChange={event => setSchedule('window_minutes', Number(event.target.value))}/></label>
        <label>首次建仓<select value={draft.schedule.initial_entry} disabled={busy} onChange={event => setSchedule('initial_entry', event.target.value)}><option value="immediate">下个可用交易时段</option><option value="scheduled">等待计划调仓日</option></select></label>
      </div>
      <p className="small muted">{mode === 'manual' ? '只手动执行时，计划只用来确定交易窗口和时区；不会自动调仓。' : '自动处理夏令时与休市顺延；窗口外等待，不补发上一窗口订单。'}</p>
    </>,
    () => <>
      <h2>第 5 步 · 检查</h2><p className="lead">运行已保存。现在用它的账户和数据源做一次只读检查，不会下单。</p>
      <div className="check-rows">
        <div className="list-row"><div className="list-main"><strong>账户连接</strong><small>{checkJob ? checkJob.message : check.phase === 'failed' && !check.checkJobId ? '检查没有开始，请重试。' : '正在检查账户连接…'}</small></div><Tag tone={!checkJob ? 'muted' : checkJob.status === 'complete' ? 'success' : 'amber'}>{!checkJob ? (check.checkJobId ? '检查中' : '等待') : checkJob.status === 'complete' ? '通过' : '未通过'}</Tag></div>
        <div className="list-row"><div className="list-main"><strong>选股预览</strong><small>{preview ? `${preview.targets.length} 个标的 · 数据截至 ${preview.data_as_of || '—'} · 目标仓位 ${percent(preview.account_target_gross)}` : previewJob ? previewJob.message : check.previewRequested ? '正在用已有数据预览选股…' : '等待账户检查完成'}</small></div><Tag tone={!previewJob ? 'muted' : preview ? 'success' : 'amber'}>{!previewJob ? (check.previewRequested ? '预览中' : '等待') : preview ? '通过' : '未通过'}</Tag></div>
      </div>
      {preview?.targets?.length > 0 && <div className="table-wrap"><table><thead><tr><th>标的</th><th className="right">策略内比例</th><th className="right">账户目标比例</th></tr></thead><tbody>{preview.targets.slice(0, 8).map(target => <tr key={target.ticker}><td><strong>{target.ticker}</strong></td><td className="right">{percent(target.weight)}</td><td className="right">{percent(target.account_weight)}</td></tr>)}</tbody></table></div>}
      {checksDone && (checkJob?.status !== 'complete' || !preview) && <p className="inline-warning">检查没有全部通过。可以先保存这个运行，去「连接」处理后在运行详情里重新检查；也可以继续到下一步，但启动时仍会先检查。</p>}
      <div className="button-row"><Button disabled={busy || !checksDone} onClick={() => runChecks(instanceId)}>重新检查</Button></div>
    </>,
    () => <>
      <h2>第 6 步 · 启动</h2><p className="lead">核对一遍，然后{mode === 'manual' ? '执行一轮，或者先放着' : '启动，之后按计划自动运行'}。关闭网页后后台继续运行。</p>
      <div className="instance-facts">
        <div><span>运行</span><strong>{item?.name || draft.name}</strong></div>
        <div><span>策略</span><strong>{strategy?.name || draft.strategy.type}</strong></div>
        <div><span>账户</span><strong>{broker?.name}</strong><small>{broker?.mode_label}</small></div>
        <div><span>数据源</span><strong>{source?.name}</strong></div>
        <div><span>资金比例</span><strong>{percent(Number(draft.budget) / 100)}</strong></div>
        <div><span>计划</span><strong>{mode === 'manual' ? '只手动执行' : scheduleSummary(draft.schedule)}</strong>{mode !== 'manual' && <small>{draft.schedule.initial_entry === 'scheduled' ? '首次建仓等计划日' : '首次建仓在下个可用交易时段'}</small>}</div>
      </div>
      {checkJob && checkJob.status !== 'complete' && <p className="inline-warning">账户检查未通过：{checkJob.message}</p>}
      {broker?.execution_enabled === false && <p className="inline-warning">{broker.execution_reason || '此账户尚未开放执行；运行已保存，授权后再启动。'}</p>}
      <p className="small muted">{mode === 'manual' ? '执行一轮会先检查账户与数据，进入可用交易窗口后执行；完成后保持停止。' : '启动后立即检查账户与数据，在交易窗口内建仓，之后按计划调仓；异常和未确认订单会暂停后续交易。'}</p>
    </>,
  ];

  const last = step === WIZARD_STEPS.length - 1;
  const canStart = broker?.execution_enabled !== false && !busy;
  return <section className="card wizard-card">
    {stepper}
    {content[step]()}
    {error && <p className="inline-warning" role="alert">{error}</p>}
    <div className="wizard-footer">
      <button type="button" className="text-link" onClick={() => navigate(instanceId ? { page: 'runs', view: 'instances', instance: instanceId } : 'instances')}>{instanceId ? '先不启动，查看这个运行' : '取消'}</button>
      <div className="button-row">
        {step > 0 && step < 4 && <Button disabled={busy} onClick={() => { setError(''); setStep(step - 1); }}>上一步</Button>}
        {step < 4 && <Button primary disabled={busy} onClick={next}>{step === 3 ? '保存并检查' : `下一步 · ${WIZARD_STEPS[step + 1][1]}`}</Button>}
        {step === 4 && <Button primary disabled={busy || !checksDone} onClick={() => setStep(5)}>下一步 · 启动</Button>}
        {last && mode === 'manual' && <Button primary disabled={!canStart} title={broker?.execution_reason} onClick={() => launch('once')}>执行一轮</Button>}
        {last && mode !== 'manual' && <Button primary disabled={!canStart} title={broker?.execution_reason} onClick={() => launch('start')}>确认启动</Button>}
      </div>
    </div>
    {instanceId && step < 5 && <p className="small muted">运行已保存为「{item?.name || draft.name}」。</p>}
  </section>;
}
