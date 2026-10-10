import React, { useEffect, useState } from 'react';
import { api } from '../api';
import { Tag } from '../ui';
import { number, percent, time, decimal } from '../format';
import { PerformanceChart } from './PerformanceChart';

const BASIS = { fill: '成交均价', observation: '观测价' };

// Market value and profit of one instance's share of the account; recomputed from retained records on open.
export function InstanceAttribution({ item }) {
  const [report, setReport] = useState(null), [error, setError] = useState('');
  useEffect(() => {
    let live = true;
    setReport(null); setError('');
    api(`strategy-instances/${item.id}/attribution`).then(value => { if (live) setReport(value); })
      .catch(failure => { if (live) setError(failure.message); });
    return () => { live = false; };
  }, [item.id, item.last_run?.at]);
  if (error) return <p className="inline-warning">{error}</p>;
  if (!report) return <p className="small muted">正在从运行记录与账户观测计算…</p>;
  const value = report.valuation;
  const tone = value?.pnl > 0 ? 'gain' : value?.pnl < 0 ? 'loss' : '';
  return <div className="instance-attribution">
    {report.message && <p className="small muted" role="status">{report.message}</p>}
    {value && <>
      <div className="instance-facts">
        <div><span>持仓市值 {value.currency || ''}</span><strong>{number(value.value)}</strong></div>
        <div><span>投入成本</span><strong>{number(value.invested)}</strong></div>
        <div><span>累计盈亏</span><strong className={tone}>{value.pnl > 0 ? '+' : ''}{number(value.pnl)}</strong></div>
        <div><span>收益率</span><strong className={tone}>{value.return_rate == null ? '—' : percent(value.return_rate)}</strong></div>
      </div>
      <p className="small muted">按 {time(value.as_of)} 的账户观测估值{value.stale ? '；最近一轮调仓晚于该观测' : ''}{value.unpriced?.length ? `；未取得价格：${value.unpriced.join('、')}` : ''}。</p>
      {report.curve.length > 1 && <PerformanceChart points={report.curve.map(p => ({ as_of: p.as_of, equity: p.value }))} mode="equity" timezone="UTC" currency={value.currency || ''} daily/>}
      {value.positions.length > 0 && <div className="table-wrap"><table><thead><tr><th>标的</th><th className="right">归属数量</th><th className="right">最新价</th><th className="right">市值</th></tr></thead><tbody>
        {value.positions.map(p => <tr key={p.ticker}><td>{p.ticker}</td><td className="right">{decimal(p.quantity)}</td><td className="right">{number(p.last_price)}</td><td className="right">{number(p.value)}</td></tr>)}
      </tbody></table></div>}
    </>}
    {report.trades.length > 0 && <details className="details"><summary>股数变化与计价 · {report.trades.length} 条</summary><div className="table-wrap"><table><thead><tr><th>时间</th><th>标的</th><th className="right">数量变化</th><th className="right">价格</th><th className="right">金额</th><th>依据</th></tr></thead><tbody>
      {report.trades.map((t, i) => <tr key={i}><td>{time(t.at)}</td><td>{t.ticker}</td><td className="right">{Number(t.delta) > 0 ? '+' : ''}{decimal(t.delta)}</td><td className="right">{number(t.price)}</td><td className="right">{number(t.cost)}</td><td>{t.basis ? <Tag tone={t.basis === 'fill' ? 'green' : 'muted'}>{BASIS[t.basis]}</Tag> : <Tag tone="amber">无价格</Tag>}</td></tr>)}
    </tbody></table></div></details>}
    <details className="details"><summary>口径说明</summary><ul className="small muted">{report.limitations.map((text, i) => <li key={i}>{text}</li>)}</ul></details>
  </div>;
}
