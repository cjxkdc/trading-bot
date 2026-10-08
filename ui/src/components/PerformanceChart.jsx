import React, { useEffect, useRef, useState } from 'react';
import { timezoneLabel } from '../execution_context';
import { chartLayout, day, observedTime, money, rate, signedMoney, tone } from '../account_report';

export function PerformanceChart({ points, mode, timezone, currency }) {
  const [selected, setSelected] = useState(null), [width, setWidth] = useState(960);
  const host = useRef(null);
  useEffect(() => { const update = () => setWidth(Math.max(320, host.current?.clientWidth || 960)); update(); const observer = new ResizeObserver(update); observer.observe(host.current); return () => observer.disconnect(); }, []);
  useEffect(() => setSelected(null), [mode, points]);
  const layout = chartLayout(points, mode, width);
  const point = layout?.dots[Math.min(selected ?? layout.dots.length - 1, layout.dots.length - 1)];
  const format = mode === 'return_rate' ? rate : mode === 'profit' ? signedMoney : money;
  return <div className="performance-chart" ref={host}>
    {!layout ? <div className="chart-no-data"><strong>收益等待资金核对</strong><span>完成下方资金核对后，这里会显示投资收益走势。净资产记录可以直接查看。</span></div> : <>
      <div className="chart-point"><time>{observedTime(point.as_of, timezone)} · {timezoneLabel(timezone)}</time><strong className={mode === 'equity' ? '' : tone(point[mode])}>{format(point[mode])}{mode !== 'return_rate' && <small> {currency}</small>}</strong></div>
      <svg viewBox={`0 0 ${layout.width} ${layout.height}`} role="img" tabIndex="0" aria-label={`${mode === 'equity' ? '净资产' : mode === 'profit' ? '投资盈亏' : '收益率'}走势，${layout.dots.length} 个观测点；方向键查看数值。`}
        onPointerMove={event => { const bounds = event.currentTarget.getBoundingClientRect(); const x = (event.clientX - bounds.left) * layout.width / bounds.width; setSelected(layout.dots.reduce((best, p, i, all) => Math.abs(p.x - x) < Math.abs(all[best].x - x) ? i : best, 0)); }}
        onPointerLeave={() => setSelected(null)} onKeyDown={event => { if (['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) { event.preventDefault(); const index = selected ?? layout.dots.length - 1; setSelected(event.key === 'Home' ? 0 : event.key === 'End' ? layout.dots.length - 1 : Math.max(0, Math.min(layout.dots.length - 1, index + (event.key === 'ArrowRight' ? 1 : -1)))); } }}>
        {layout.ticks.map((tick, i) => <g key={i}><line className="candle-grid" x1={layout.left} x2={layout.right} y1={tick.y} y2={tick.y}/><text className="candle-axis" x={layout.right + 10} y={tick.y + 4}>{format(tick.value)}</text></g>)}
        <path className="performance-line" d={layout.path}/><line className="candle-selection" x1={point.x} x2={point.x} y1={layout.top} y2={layout.bottom}/><circle className="performance-dot" cx={point.x} cy={point.y} r="4"/>
        <text className="candle-axis" x={layout.left} y={layout.height - 5}>{day(layout.dots[0].as_of, timezone)}</text>{layout.dots.length > 1 && <text className="candle-axis" textAnchor="end" x={layout.right} y={layout.height - 5}>{day(layout.dots.at(-1).as_of, timezone)}</text>}
      </svg>
      <details className="chart-values"><summary>查看数值</summary><div className="table-wrap"><table><thead><tr><th>观测时间 · {timezoneLabel(timezone)}</th><th className="right">{mode === 'return_rate' ? '收益率' : `金额 · ${currency}`}</th></tr></thead><tbody>{[...layout.dots].reverse().map(p => <tr key={p.as_of}><td>{observedTime(p.as_of, timezone)}</td><td className="right">{format(p[mode])}</td></tr>)}</tbody></table></div></details>
    </>}
  </div>;
}
