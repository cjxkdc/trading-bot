import React, { useState } from 'react';
import { api } from '../api';
import { Button, Empty, Tag } from '../ui';
import { number } from '../format';
import { DATE_LABELS, QUEUE_LABELS, backfillPercent, backfillSummary, cadenceLabel, defaultBackfillRange, durationLabel } from '../backfill';

// Queued historical collection for the local replay series; the backend runs one date per idle slot.
export function BackfillCard({ state, strategyType, busy, act }) {
  const [range, setRange] = useState(defaultBackfillRange);
  const [plan, setPlan] = useState(null);
  const queue = state.backfill;
  const source = state.sources.find(item => item.id === state.source);
  const supported = source?.capabilities?.includes('backfill');
  const active = queue && ['running', 'paused'].includes(queue.status);
  const manualMismatch = strategyType !== state.strategy.type;
  const update = changes => { setRange({ ...range, ...changes }); setPlan(null); };

  async function estimate(event) {
    event.preventDefault();
    const result = await act(() => api('backfill/plan', { ...range, expected_strategy_type: strategyType }), '', { refreshAfter: false });
    if (result) setPlan(result);
  }
  async function start() {
    const result = await act(() => api('backfill', { ...range, expected_strategy_type: strategyType }));
    if (result) setPlan(null);
  }
  const control = command => act(() => api(`backfill/${command}`, {}));

  return <section className="card">
    <div className="card-heading"><div><h2>历史回填</h2><p>按当前策略的数据需求，向 Massive 逐个日期补采过去的快照，为本地回放补出更长的序列。每个日期一份快照，记录前一交易日收盘时的股票池、行情与已申报的财报。</p></div><Tag>Massive</Tag></div>
    {!supported ? <Empty title="当前数据源不支持历史回填" icon="database">只有 Massive 数据源能按日期补采历史快照。在「连接 → 数据源」切换到 Massive 并配置密钥后，这里可以回填。</Empty>
    : active ? <BackfillProgress queue={queue} busy={busy} control={control}/>
    : <form onSubmit={estimate}>
      {queue && <p className="small muted">上次回填 {backfillSummary(queue)}</p>}
      <div className="backtest-form">
        <label>开始日期<input type="date" required value={range.start} disabled={busy} onChange={event => update({ start: event.target.value })}/></label>
        <label>结束日期<input type="date" required value={range.end} disabled={busy} onChange={event => update({ end: event.target.value })}/></label>
        <label>快照周期<select value={range.cadence} disabled={busy} onChange={event => update({ cadence: event.target.value })}><option value="monthly">每月（每月 1 日）</option><option value="weekly">每周（周一）</option></select></label>
        <Button type="submit" disabled={busy || manualMismatch}>估算请求量</Button>
      </div>
      {manualMismatch && <p className="inline-warning">历史回填按「参数」页保存的手动配置采集；当前保存的是 {state.strategy_name}。先在参数页保存这个策略，再回填。</p>}
      {plan && <div className="backfill-plan">
        <p>共 {plan.dates.length} 个日期，其中 {plan.pending_count} 个需要采集{plan.skipped_count ? `，${plan.skipped_count} 个已有快照跳过` : ''}。预计约 <strong>{number(plan.estimate.requests)}</strong> 次请求，按 {plan.estimate.requests_per_minute} 次/分钟约需 <strong>{durationLabel(plan.estimate.minutes)}</strong>。</p>
        <p className="small muted">估算按每个日期 {plan.estimate.details_per_date} 只候选股的资料{plan.estimate.income_required ? '与财报' : ''}、首个日期 {plan.estimate.history_bars} 个交易日的行情计算；已缓存的请求不再重复，实际通常更少。队列在工作台空闲时逐个日期采集，可随时暂停或取消，已完成的快照保留。</p>
        <div className="button-row"><Button primary disabled={busy || !plan.pending_count} onClick={start}>开始回填 {plan.pending_count} 个日期</Button></div>
      </div>}
    </form>}
    <details className="details"><summary>回填的口径与成本</summary><p className="small muted">每个日期的快照按供应商的时点参数查询当日的股票池与基础资料，财报按申报日截止，行情取当日之前的历史日线。市值等字段是否严格为当日时点取决于供应商，当日已退市的股票可能缺失，所以这些快照仍标记为研究数据，不会被选为当前数据，也不能用于下单。请求受数据套餐的速率限制；回填期间其他数据操作会排队等待。</p></details>
  </section>;
}

function BackfillProgress({ queue, busy, control }) {
  const [label, tone] = QUEUE_LABELS[queue.status];
  return <div className="backfill-progress">
    <div className="backfill-status"><Tag tone={tone}>{label}</Tag><span>{queue.start} → {queue.end} · {cadenceLabel(queue.cadence)} · {queue.finished_count}/{queue.total} 个日期{queue.current ? ` · 正在采集 ${queue.current}` : ''}</span></div>
    <div className="server-meter" role="progressbar" aria-valuenow={backfillPercent(queue)} aria-valuemin="0" aria-valuemax="100"><i style={{ width: `${backfillPercent(queue)}%` }}/></div>
    <p className="small muted" role="status">{queue.message}</p>
    <div className="button-row">
      {queue.status === 'running' && <Button disabled={busy} onClick={() => control('pause')}>暂停</Button>}
      {queue.status === 'paused' && <Button primary disabled={busy} onClick={() => control('resume')}>继续</Button>}
      <Button disabled={busy} onClick={() => control('cancel')}>取消回填</Button>
    </div>
    <details className="details"><summary>日期明细</summary><div className="table-wrap"><table><thead><tr><th>日期</th><th>状态</th><th>价格日期</th><th className="right">可用股票</th><th>说明</th></tr></thead><tbody>
      {queue.dates.map(item => { const [text, color] = DATE_LABELS[item.status] || [item.status, 'muted']; return <tr key={item.as_of}><td>{item.as_of}</td><td><Tag tone={color}>{text}</Tag></td><td>{item.price_as_of || '—'}</td><td className="right">{item.usable_count ?? '—'}</td><td className="small muted">{item.message}</td></tr>; })}
    </tbody></table></div></details>
  </div>;
}
