import React, { useEffect, useRef, useState } from 'react';
import { api } from '../api';
import { AccountCandles } from './AccountCandles';
import { PerformanceChart } from './PerformanceChart';
import { periods, money, signedMoney, rate, tone, day, observedTime, finite, visiblePositions } from '../account_report';
import { decimal, time } from '../format';
import { executionContext, currencyLabel, timezoneLabel } from '../execution_context';

const kinds = { deposit: '入金', withdrawal: '出金', dividend: '股息（已计入净资产）', fee: '费用（已计入净资产）', reset: '重置统计起点' };

export function PerformancePanel({ state, busy, run, act, chooseBroker, navigate }) {
  const [report, setReport] = useState(null), [error, setError] = useState('');
  const [period, setPeriod] = useState('ALL'), [mode, setMode] = useState('equity');
  const [review, setReview] = useState(false), [query, setQuery] = useState(''), [sort, setSort] = useState('market_value');
  const [flow, setFlow] = useState({ as_of: '', kind: 'deposit', amount: '', note: '' });
  const flowDetails = useRef(null), generation = useRef(0);
  const broker = state.broker.id;
  async function load() { const id = ++generation.current; try { const result = await api('performance'); if (id === generation.current) { setReport(result); setError(''); } } catch (failure) { if (id === generation.current) setError(failure.message); } }
  useEffect(() => {
    const id = ++generation.current; setReview(false);
    api('performance').then(data => { if (id === generation.current) { setReport(data); setError(''); } }).catch(e => { if (id === generation.current) setError(e.message); });
    return () => { generation.current++; };
  }, [broker, state.monitoring?.last_success, state.jobs[0]?.id, state.jobs[0]?.status]);
  const current = report?.broker_id === broker ? report : null, latest = current?.latest;
  const context = executionContext(state.broker, { currency:current?.currency || latest?.currency, timezone:current?.timezone || latest?.timezone });
  const currency = currencyLabel(context.currency), timezone = context.timezone, zone = timezoneLabel(timezone);
  const analysis = current?.analytics?.[period], summary = current?.position_summary || {};
  const complete = analysis?.metrics?.profit != null, stats = complete ? analysis.metrics : analysis?.verified || {};
  const stale = latest && Date.now() - Date.parse(latest.as_of) > (state.monitoring?.settings?.interval_seconds || 900) * 2000;
  const held = visiblePositions(current?.positions, query, sort);
  function openFlows() { if (flowDetails.current) { flowDetails.current.open = true; flowDetails.current.scrollIntoView({ behavior: 'smooth', block: 'start' }); } }
  async function submitFlow(event) {
    event.preventDefault();
    const result = await act(() => api('performance/cash-event', { ...flow, amount: Number(flow.amount), as_of: new Date(flow.as_of).toISOString(), view_token: current.view_token }));
    if (result) { setFlow({ ...flow, amount: '', note: '' }); setReview(false); await load(); }
  }
  async function reviewFlows() {
    const result = await act(() => api('performance/review', { through: latest.as_of, view_token: current.view_token }));
    if (result) { setReview(false); await load(); }
  }
  return <div className="account-overview">
    <div className="account-heading"><div><select className="account-selector" aria-label="查看账户" value={broker} disabled={busy} onChange={e => chooseBroker(e.target.value)}>{state.brokers.map(item => <option key={item.id} value={item.id} disabled={!item.enabled}>{item.name}</option>)}</select><p>{latest ? `${time(latest.as_of)} 更新${stale ? ' · 数据待更新' : ''}` : current?.message || '首次更新后开始记录账户表现。'}</p></div><div className="button-row"><button className="text-link" onClick={() => navigate('broker')}>连接设置</button><button className="button" disabled={busy || !state.actions?.observe?.enabled} title={state.actions?.observe?.message} onClick={() => run('observe')}>刷新账户</button></div></div>
    {error && <p className="inline-warning" role="alert">{error}</p>}{state.monitoring?.error && <p className="inline-warning">{state.monitoring.error}</p>}
    {!current ? <section className="card chart-no-data" role="status"><strong>{error ? '暂时无法读取账户记录' : '正在读取账户记录…'}</strong><span>{error ? '连接恢复后会重新读取。' : '正在获取资产、持仓和收益表现。'}</span></section> : !latest ? <section className="card chart-no-data"><strong>还没有账户资产记录</strong><span>{current.message || '连接账户后点击「刷新账户」，查看资产、持仓和盈亏。'}</span></section> : <>
      <section className="card asset-summary" aria-label="账户资产摘要">
        <div className="asset-primary"><span>净资产 <small>{currency}</small></span><strong>{money(latest.equity)}</strong><span className="small muted">{summary.position_count ?? '—'} 只持仓 · {latest.open_order_count ?? '—'} 笔未完成委托</span></div>
        <dl className="asset-metrics"><div><dt>持仓市值</dt><dd>{money(latest.market_value)}</dd></div><div><dt>总现金</dt><dd>{money(latest.cash_total)}</dd></div><div><dt>持仓盈亏</dt><dd className={tone(summary.position_pnl)}>{signedMoney(summary.position_pnl)}</dd><small>{summary.position_pnl == null ? '部分持仓未提供有效盈亏' : '券商持仓口径'}</small></div><div><dt>下单可用现金</dt><dd>{money(latest.cash_available)}</dd></div></dl>
      </section>
      <section className="card return-section">
        <div className="return-heading"><h2>收益表现</h2>{mode !== 'candles' && <div className="segmented" role="group" aria-label="收益统计区间">{periods.map(([id, label]) => <button key={id} aria-pressed={period === id} onClick={() => setPeriod(id)}>{label}</button>)}</div>}</div>
        {mode !== 'candles' && <><div className="return-stats"><div><span>{complete ? '区间投资盈亏' : analysis?.verified ? '已核对投资盈亏' : '区间投资盈亏'}</span><strong className={tone(stats.profit)}>{signedMoney(stats.profit)}<small> {currency}</small></strong></div><div><span>收益率</span><strong className={tone(stats.return_rate)}>{rate(stats.return_rate)}</strong></div><div><span>最大回撤</span><strong>{stats.max_drawdown == null ? '—' : `${(stats.max_drawdown * 100).toFixed(2)}%`}</strong></div></div>
        <p className="range-caption">{analysis?.start ? `${day(analysis.start, timezone)} — ${day(complete ? analysis.end : analysis.verified?.through || analysis.end, timezone)} · ${zone} · 实际观测区间` : '等待历史记录'}{analysis?.observations === 1 && ' · 只有一个观测点，尚不能计算区间收益'}</p></>}
        <div className="chart-mode-row"><div className="line-tabs" role="group" aria-label="账户走势图">{[['equity', '净资产'], ['return_rate', '收益率'], ['profit', '盈亏金额'], ['candles', 'K 线']].map(([id, label]) => <button key={id} aria-pressed={mode === id} onClick={() => setMode(id)}>{label}</button>)}</div><span className="small muted">{mode === 'equity' || mode === 'candles' ? '包含出入金影响' : '已扣除已登记的出入金'}</span></div>
        {mode === 'candles' ? <AccountCandles key={`${broker}:${current.account_key || ''}`} brokerId={broker} accountKey={current.account_key} revision={`${current.view_token}:${(current.cash_events || []).map(f => f.id).join(',')}`} intervalSeconds={state.monitoring?.settings?.interval_seconds} currency={currency} timezone={timezone}/> : <PerformanceChart points={analysis?.points || []} mode={mode} currency={currency} timezone={timezone}/>}
        {current.metrics?.flow_review_required && <div className="cash-review-note"><span>{analysis?.verified ? `投资收益已核对至 ${observedTime(analysis.verified.through, timezone)} ${zone}，最新观测仍需核对资金变化。` : '核对记录期间的出入金后，可计算投资收益；持仓盈亏与净资产已可查看。'}</span><button className="text-link" onClick={openFlows}>核对资金 →</button></div>}
        {current.metrics?.benchmark_period && <details className="chart-values"><summary>基准对照</summary><p className="small muted">{current.metrics.benchmark_period.start} — {current.metrics.benchmark_period.end}：{current.metrics.benchmark_period.symbol} 价格收益 {rate(current.metrics.benchmark_return)}，同期账户收益 {rate(current.metrics.benchmark_period.account_return)}。仅比较匹配收盘日期，基准不含股息。</p></details>}
      </section>
      <section className="card holdings-section"><div className="holdings-heading"><h2>我的持仓 <small>{summary.position_count ?? 0}</small></h2><div><input type="search" aria-label="搜索持仓" placeholder="搜索标的代码或名称" value={query} onChange={e => setQuery(e.target.value)}/><select aria-label="持仓排序" value={sort} onChange={e => setSort(e.target.value)}><option value="market_value">按市值</option><option value="position_pnl">按盈亏</option><option value="weight">按权重</option></select></div></div>
        {!held.length ? <p className="empty-holdings">{query ? '没有匹配的持仓' : '当前没有持仓'}</p> : <div className="table-wrap"><table className="holdings-table"><thead><tr><th>标的</th><th className="right">数量</th><th className="right">现价 / 成本</th><th className="right">市值 / 权重</th><th className="right">持仓盈亏 / 比例</th></tr></thead><tbody>{held.map(p => <tr key={p.symbol}><td><strong>{p.symbol}</strong><small>{currencyLabel(p.currency || context.currency)}</small><small>{p.name || p.market || context.market || '—'}</small></td><td className="right">{decimal(p.quantity)}</td><td className="right">{decimal(p.last_price)}<small>{decimal(p.cost_price)}</small></td><td className="right">{money(p.market_value)}<small>{finite(p.weight) ? `${(p.weight * 100).toFixed(1)}%` : '—'}</small></td><td className={`right ${tone(p.position_pnl)}`}><strong>{signedMoney(p.position_pnl)}</strong><small className={tone(p.pnl_rate)}>{rate(p.pnl_rate)}</small></td></tr>)}</tbody></table></div>}
        <p className="small muted table-caption">账户统计币种：{currency}。持仓按各自币种标记；成本及盈亏采用券商口径，未提供的字段显示「—」。</p>
        <details className="chart-values"><summary>与最近一次调仓目标比较</summary><div className="table-wrap"><table><thead><tr><th>标的</th><th className="right">当前权重</th><th className="right">上次执行目标</th><th className="right">差额</th></tr></thead><tbody>{(current.positions || []).map(p => <tr key={p.symbol}><td>{p.symbol}</td><td className="right">{rate(p.weight)}</td><td className="right">{rate(p.target_weight)}</td><td className="right">{rate(p.drift)}</td></tr>)}</tbody></table></div></details>
      </section>
      <details className="card account-details" ref={flowDetails}><summary>资金流水与收益核对 <span>{current.metrics?.flow_review_required ? '待核对' : '已核对'}</span></summary>
        <p className="small muted">请登记本统计区间的全部入金、出金及账户重置。股息与费用已体现在净资产中，不再作为出入金扣除。</p>
        <form onSubmit={submitFlow} className="monitor-form"><label>发生时间 · 本地<input type="datetime-local" required value={flow.as_of} onChange={e => setFlow({ ...flow, as_of: e.target.value })}/></label><label>类型<select value={flow.kind} onChange={e => setFlow({ ...flow, kind: e.target.value })}>{Object.entries(kinds).map(([id, label]) => <option key={id} value={id}>{label}</option>)}</select></label><label>金额 · {currency}<input type="number" min="0" step="0.01" required value={flow.amount} onChange={e => setFlow({ ...flow, amount: e.target.value })}/></label><label>备注<input maxLength="500" value={flow.note} onChange={e => setFlow({ ...flow, note: e.target.value })}/></label><button className="button" disabled={busy}>登记</button></form>
        {(current.cash_events || []).map(item => <div className="log-line" key={item.id}><time>{time(item.as_of)}</time><span>{kinds[item.kind]} · {money(item.amount)} · {item.note}</span><button className="text-link" disabled={busy} onClick={async () => { const r = await act(() => api('performance/cash-event/remove', { id: item.id, view_token: current.view_token })); if (r) await load(); }}>移除错误记录</button></div>)}
        <label className="review-check"><input type="checkbox" checked={review} onChange={e => setReview(e.target.checked)}/>已核对本区间，全部出入金和重置均已登记（没有则无需登记）。</label><button className="button" disabled={busy || !review} onClick={reviewFlows}>确认核对并计算收益</button><p className="small muted">最近核对：{time(current.flow_reviewed_through)}。后续未核对的观测仅展示净资产，已核对收益仍可查看。</p>
      </details>
      <details className="card account-details"><summary>调仓复盘 <span>{current.cycles?.length || 0} 次</span></summary>{current.cycles?.map(c => <article key={c.decision_id} className="rebalance-review"><strong>{time(c.recorded_at)} · {c.status === 'complete' ? '执行完成' : '尚未确认完成'}</strong><p>{c.message}</p><div className="table-wrap"><table><thead><tr><th>标的</th><th>方向</th><th>委托数量</th><th>已成交</th><th>均价</th></tr></thead><tbody>{(c.fills?.length ? c.fills : c.orders || []).map((o, i) => <tr key={o.id || i}><td>{o.ticker || o.symbol}</td><td>{o.side === 'BUY' ? '买入' : '卖出'}</td><td>{decimal(o.quantity)}</td><td>{decimal(o.filled_quantity)}</td><td>{decimal(o.average_price)}</td></tr>)}</tbody></table></div></article>)}</details>
      <details className="account-method"><summary>统计口径与数据说明</summary><p>投资盈亏 = 区间末净资产 − 区间初净资产 − 净入金。收益率使用按观测区间连接的 Modified Dietz 估算；回撤基于这些收益观测点。区间按 {zone} 筛选，并标明实际记录起止时间，不补造历史。</p><ul>{latest.limitations?.map((text, i) => <li key={i}>{text}</li>)}</ul></details>
    </>}
  </div>;
}
