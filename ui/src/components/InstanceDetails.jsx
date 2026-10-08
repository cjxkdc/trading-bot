import React, { useState } from 'react';
import { api, downloadCode } from '../api';
import { scheduleSummary } from '../automation';
import { budgetSummary, instanceDraft, instancePayload, instanceStatus, runnableBroker, latestInstancePreview } from '../strategy_instances';

import { ModuleFields } from './ModuleFields';
import { StrategyReadme } from './StrategyReadme';
import { percent, decimal, time } from '../format';


export function InstanceDetails({ state, item, busy, act }) {
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
      {preview.targets.length ? <div className="table-wrap"><table><thead><tr><th>标的</th><th>得分</th><th>策略内比例</th><th>账户目标比例</th></tr></thead><tbody>{preview.targets.map(x => <tr key={x.ticker}><td>{x.ticker}</td><td>{Number(x.score).toFixed(3)}</td><td>{percent(x.weight)}</td><td>{percent(x.account_weight)}</td></tr>)}</tbody></table></div> : <p className="small muted">当前数据下没有选中标的。</p>}
      <p className="small muted">本实例目标标的仓位 {percent(preview.account_target_gross)}。只计算选股和比例，未读取券商报价；符合交易规则的数量与订单在运行时重新计算。</p></section>}
    <details className="details"><summary>参数与策略说明</summary><pre className="instance-json">{JSON.stringify(item.strategy, null, 2)}</pre><StrategyReadme content={strategy?.readme?.content} filename={strategy?.readme?.path} issue={strategy?.readme_issue}/></details>
    <details className="details"><summary>持仓归属 · {item.positions.length} 只</summary>{item.positions.length ? <div className="table-wrap"><table><thead><tr><th>标的</th><th>内部归属数量</th></tr></thead><tbody>{item.positions.map(x => <tr key={x.ticker}><td>{x.ticker}</td><td>{decimal(x.quantity)}</td></tr>)}</tbody></table></div> : <p className="small muted">尚无已确认的实例持仓。</p>}<p className="small muted">同一标的可属于多份实例；券商展示合并持仓，内部归属在确认成交后更新。</p></details>
    <details className="details"><summary>运行日志</summary>{item.events.map((event, i) => <div className="log-line" key={i}><time>{time(event.at)}</time><span>{event.message}</span></div>)}<button className="button" disabled={busy} onClick={() => act(async () => { const result = await api(`strategy-instances/${item.id}/logs`); setLogs(result.items); return null; })}>读取执行记录</button>{logs?.map(log => <details className="details" key={log.cycle_id}><summary>{time(log.created_at)} · {log.message}</summary><pre className="instance-json">{JSON.stringify(log, null, 2)}</pre></details>)}</details>
    <button className="text-link" onClick={() => setExporting(!exporting)}>按此实例导出回测</button>{exporting && <form className="instance-export" onSubmit={exportCode}><label>开始日期<input type="date" required value={range.start} onChange={e => setRange({ ...range, start: e.target.value })}/></label><label>结束日期<input type="date" required value={range.end} onChange={e => setRange({ ...range, end: e.target.value })}/></label><label>滑点 (bps)<input type="number" min="0" max="9999" value={range.slippage_bps} onChange={e => setRange({ ...range, slippage_bps: Number(e.target.value) })}/></label><button className="button" disabled={busy}>导出 QuantConnect 代码</button><p className="small muted">单独回测此实例的策略和计划；多策略账户的合并表现从实际运行记录观察。</p></form>}
  </div>;
}
