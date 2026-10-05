import React, { useEffect, useState } from 'react';
import { api } from '../api';

const money = value => value == null ? '未提供' : Number(value).toLocaleString('zh-CN', { maximumFractionDigits: 2 });
const pct = value => value == null ? '待核实' : `${(Number(value)*100).toFixed(2)}%`;
const time = value => value ? new Date(value).toLocaleString('zh-CN', { hour12:false }) : '尚无记录';
const kinds = { deposit:'入金', withdrawal:'出金', dividend:'股息（已计入净资产）', fee:'费用（已计入净资产）', reset:'重置统计起点' };

function EquityChart({ points }) {
  if (points.length < 2) return <p className="small muted">取得第二个观测点后显示曲线。</p>;
  const values = points.map(p => p.equity), low = Math.min(...values), high = Math.max(...values);
  const start = Date.parse(points[0].as_of), end = Date.parse(points.at(-1).as_of);
  const line = points.map(p => `${20+760*(Date.parse(p.as_of)-start)/(end-start || 1)},${150-125*(p.equity-low)/(high-low || 1)}`).join(' ');
  return <figure className="equity-chart"><svg viewBox="0 0 800 175" role="img" aria-label="美元账户净资产历史曲线，包含资金转入转出影响"><line x1="20" y1="150" x2="780" y2="150" stroke="#dbe3ee"/><polyline fill="none" stroke="#4874d5" strokeWidth="2.5" points={line}/><text x="20" y="15">USD {money(high)}</text></svg><figcaption><span>{time(points[0].as_of)}</span><span>{time(points.at(-1).as_of)}</span></figcaption></figure>;
}

export function PerformancePanel({ state, busy, run, act, compact=false }) {
  const [report, setReport] = useState(null), [error,setError] = useState('');
  const [review,setReview] = useState(false);
  const [flow,setFlow] = useState({ as_of:'', kind:'deposit', amount:'', note:'' });
  const broker = state.broker.id;
  async function load() { const result = await api('performance'); setReport(result); setError(''); return result; }
  useEffect(() => {
    let live=true;
    setReview(false);
    api('performance').then(data => { if(live) { setReport(data); setError(''); } }).catch(e => { if(live) setError(e.message); });
    return () => { live=false; };
  }, [broker,state.monitoring?.last_success,state.jobs[0]?.id,state.jobs[0]?.status]);
  const current = report?.broker_id === broker ? report : null;
  const latest = current?.latest, metrics = current?.metrics || {};
  const stale = latest && Date.now()-Date.parse(latest.as_of) > (state.monitoring?.settings?.interval_seconds || 900)*2000;
  if (compact && !state.broker.capabilities.includes('observe')) return null;
  async function submitFlow(event) {
    event.preventDefault();
    const result=await act(() => api('performance/cash-event',{...flow, amount:Number(flow.amount), as_of:new Date(flow.as_of).toISOString(), view_token:current.view_token}));
    if(result) { setFlow({...flow,amount:'',note:''}); setReview(false); await load(); }
  }
  async function reviewFlows() {
    const result=await act(() => api('performance/review',{through:latest.as_of,view_token:current.view_token}));
    if(result) { setReview(false); await load(); }
  }
  return <section className="card performance-panel">
    <div className="card-heading"><div><h2>账户表现</h2><p>{latest ? `${state.broker.name} · ${time(latest.as_of)}${stale ? ' · 等待更新' : ''}` : current?.message || '首次检查后开始积累账户记录。'}</p></div><button className="button" disabled={busy || !state.actions?.observe?.enabled} title={state.actions?.observe?.message} onClick={() => run('observe')}>更新账户表现</button></div>
    {error && <p className="inline-warning">{error}</p>}
    {state.monitoring?.error && <p className="inline-warning">{state.monitoring.error}</p>}
    {latest && <><div className="result-metrics performance-metrics"><div><span>净资产 · USD</span><strong>{money(latest.equity)}</strong></div><div><span>总现金 · USD</span><strong>{money(latest.cash_total)}</strong></div><div><span>资金调整后收益</span><strong>{pct(metrics.return_rate)}</strong></div><div><span>最大回撤 · 观测点</span><strong>{pct(metrics.max_drawdown)}</strong></div></div>
      {!compact && <><EquityChart points={current.curve || []}/><p className="small muted">从启用记录开始统计，每日保留最后观测点；不是成交逐笔曲线。净资产变化 {pct(metrics.asset_change)}，包含出入金影响。</p>
      {metrics.benchmark_period && <p className="small muted">{metrics.benchmark_period.start} 至 {metrics.benchmark_period.end}：{metrics.benchmark_period.symbol} 价格收益 {pct(metrics.benchmark_return)}，同期账户收益 {pct(metrics.benchmark_period.account_return)}。基准不含股息，仅比较有匹配收盘数据的日期。</p>}
      <div className="table-wrap"><table><thead><tr><th>股票</th><th className="right">数量</th><th className="right">市值 · USD</th><th className="right">当前权重</th><th className="right">上次执行目标</th><th className="right">差额</th></tr></thead><tbody>{(current.positions || []).map(row => <tr key={row.symbol}><td>{row.symbol}</td><td className="right">{money(row.quantity)}</td><td className="right">{money(row.market_value)}</td><td className="right">{pct(row.weight)}</td><td className="right">{pct(row.target_weight)}</td><td className="right">{pct(row.drift)}</td></tr>)}</tbody></table></div>
      {latest.open_order_count > 0 && <p className="inline-warning">仍有 {latest.open_order_count} 笔未完成委托，当前仓位可能继续变化。</p>}
      <details className="details"><summary>资金流水与统计口径{metrics.flow_review_required ? ' · 需要核对' : ''}</summary>
        <p className="small muted">券商接口没有在这里提供完整出入金流水。请登记本统计区间的入金、出金或模拟账户重置；股息和费用已经体现在净资产中，不作为外部资金扣除。资金调整收益使用按观测区间连接的 Modified Dietz 估算。</p>
        <form onSubmit={submitFlow} className="monitor-form"><label>发生时间 · 本地<input type="datetime-local" required value={flow.as_of} onChange={e=>setFlow({...flow,as_of:e.target.value})}/></label><label>类型<select value={flow.kind} onChange={e=>setFlow({...flow,kind:e.target.value})}>{Object.entries(kinds).map(([id,name])=><option key={id} value={id}>{name}</option>)}</select></label><label>金额 · USD<input type="number" min="0" step="0.01" required value={flow.amount} onChange={e=>setFlow({...flow,amount:e.target.value})}/></label><label>备注<input maxLength="500" value={flow.note} onChange={e=>setFlow({...flow,note:e.target.value})}/></label><button className="button" disabled={busy}>登记</button></form>
        {(current.cash_events || []).map(item=><div className="log-line" key={item.id}><time>{time(item.as_of)}</time><span>{kinds[item.kind]} · {money(item.amount)} · {item.note}</span><button className="text-link" disabled={busy} onClick={async()=>{ const r=await act(()=>api('performance/cash-event/remove',{id:item.id,view_token:current.view_token})); if(r) await load(); }}>移除错误记录</button></div>)}
        <label className="review-check"><input type="checkbox" checked={review} onChange={e=>setReview(e.target.checked)}/>我已核对上述区间的资金变化，已登记全部出入金和重置（没有则无需登记）。</label><button className="button" disabled={busy || !review} onClick={reviewFlows}>确认核对并计算收益</button>
        <p className="small muted">最近核对：{time(current.flow_reviewed_through)}。新观测产生后需再次核对；未核实的收益显示为“待核实”。</p>
      </details>
      <details className="details"><summary>最近调仓复盘（{current.cycles?.length || 0}）</summary>{current.cycles?.map(c=><article key={c.decision_id} className="rebalance-review"><strong>{time(c.recorded_at)} · {c.status === 'complete' ? '执行完成' : '已准备，未确认完成'}</strong><p>{c.message}</p><div className="table-wrap"><table><thead><tr><th>股票</th><th>方向</th><th>委托数量</th><th>已成交</th><th>成交均价</th></tr></thead><tbody>{(c.fills?.length ? c.fills : c.orders || []).map((o,i)=><tr key={o.id || i}><td>{o.ticker || o.symbol}</td><td>{o.side === 'BUY' ? '买入' : '卖出'}</td><td>{money(o.quantity)}</td><td>{money(o.filled_quantity)}</td><td>{money(o.average_price)}</td></tr>)}</tbody></table></div><small className="muted">成交状态来自本程序账本的最近核对；手续费未由券商核实。</small></article>)}</details>
      <details className="details"><summary>数据说明</summary><ul>{latest.limitations?.map((text,i)=><li key={i}>{text}</li>)}</ul></details></>}
      {compact && metrics.flow_review_required && <p className="small muted">净资产记录已开始；在「持仓与订单」核对资金流水后显示收益与回撤。</p>}
    </>}
  </section>;
}
