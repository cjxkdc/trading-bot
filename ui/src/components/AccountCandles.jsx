import React, { useEffect, useRef, useState } from 'react';
import { api } from '../api';
import { timezoneLabel, currencyLabel } from '../execution_context';
import { candlePeriods, candleLayout, nearestCandle, moveCandle, candleLabel, candleAxisLabel } from '../account_candles';

const money = value => Number(value).toLocaleString('zh-CN', { minimumFractionDigits:2, maximumFractionDigits:2 });

export function AccountCandles({ brokerId, accountKey, revision, intervalSeconds=900, currency, timezone }) {
  const [interval,setInterval] = useState('1h');
  const [data,setData] = useState(null), [error,setError] = useState(''), [loading,setLoading] = useState(true);
  const [selected,setSelected] = useState(null), [width,setWidth] = useState(960);
  const container = useRef(null);
  useEffect(() => {
    const element = container.current;
    if (!element) return;
    const resize = () => setWidth(Math.max(320,element.getBoundingClientRect().width));
    resize();
    const observer = new ResizeObserver(resize);
    observer.observe(element);
    return () => observer.disconnect();
  }, []);
  useEffect(() => {
    let live = true;
    setLoading(true); setData(null); setError(''); setSelected(null);
    const query = `performance/candles?interval=${interval}&limit=96${accountKey ? `&account_key=${encodeURIComponent(accountKey)}` : ''}`;
    api(query).then(result => {
      if (!live) return;
      if (result.broker_id !== brokerId || (accountKey && result.account_key !== accountKey)) throw new Error('账户已变化，请刷新账户表现。');
      setData(result);
    }).catch(e => { if (live) setError(e.message); }).finally(() => { if (live) setLoading(false); });
    return () => { live = false; };
  }, [brokerId,accountKey,revision,interval]);
  const bars = data?.bars || [], layout = candleLayout(bars,width);
  const point = layout?.points[selected ?? bars.length-1];
  const axisCount = width < 600 ? 2 : 5;
  const axisIndices = [...new Set(Array.from({length:axisCount},(_,i) => Math.round(i*(bars.length-1)/(axisCount-1))))];
  const sampling = data?.interval_seconds || intervalSeconds;
  const zone = data?.timezone || timezone, unit = currencyLabel(data?.currency || currency);
  return <div className="account-candles" ref={container}>
    <div className="candle-toolbar"><h3>净资产 K 线 <span>{unit}</span></h3><div className="candle-periods" role="group" aria-label="净资产 K 线周期">{candlePeriods.map(item => <button type="button" key={item.id} aria-pressed={item.id===interval} onClick={() => setInterval(item.id)}>{item.label}</button>)}</div></div>
    {loading ? <div className="candle-empty" role="status">正在读取账户记录…</div>
      : error ? <div className="candle-empty inline-warning" role="alert">{error}</div>
      : !bars.length ? <div className="candle-empty">{data?.message || '暂无这个周期的观测记录，首次采集后开始显示。'}</div>
      : !layout ? <div className="candle-empty inline-warning" role="alert">K 线记录未通过校验，请重新读取账户表现。</div>
      : <><div className="candle-readout"><strong>{candleLabel(point,interval,zone)}</strong><span>{point.in_progress ? '形成中 · ' : ''}{point.sample_count} 个观测点</span><dl>{[['开',point.open],['高',point.high],['低',point.low],['收',point.close]].map(([name,value]) => <div key={name}><dt>{name}</dt><dd>{money(value)}</dd></div>)}</dl></div>
        <svg className="candle-chart" viewBox={`0 0 ${layout.width} ${layout.height}`} role="img" tabIndex="0" aria-label={`${unit} 账户净资产 K 线，共 ${bars.length} 根。左右方向键切换观测周期。`}
          onPointerMove={e => { const box=e.currentTarget.getBoundingClientRect(); setSelected(nearestCandle(layout.points,(e.clientX-box.left)*layout.width/box.width)); }}
          onPointerLeave={() => setSelected(null)} onKeyDown={e => { if (['ArrowLeft','ArrowRight','Home','End'].includes(e.key)) { e.preventDefault(); setSelected(moveCandle(selected,e.key,bars.length)); } }}>
          {layout.ticks.map((tick,i) => <g key={i}><line className="candle-grid" x1={layout.left} x2={layout.right} y1={tick.y} y2={tick.y}/><text className="candle-axis" x={layout.right+12} y={tick.y+4}>{money(tick.value)}</text></g>)}
          <line className="candle-selection" x1={point.x} x2={point.x} y1={layout.top} y2={layout.bottom}/>
          {layout.points.map(p => <g key={p.start} className={`candle ${p.tone}${p.in_progress ? ' forming' : ''}`}><title>{`${candleLabel(p,interval,zone)}：开 ${money(p.open)}，高 ${money(p.high)}，低 ${money(p.low)}，收 ${money(p.close)}；${p.sample_count} 个观测点`}</title><line x1={p.x} x2={p.x} y1={p.yHigh} y2={p.yLow}/>{p.tone==='flat' ? <line x1={p.x-p.width/2} x2={p.x+p.width/2} y1={p.yOpen} y2={p.yOpen}/> : <rect x={p.x-p.width/2} y={Math.min(p.yOpen,p.yClose)} width={p.width} height={Math.max(1.5,Math.abs(p.yOpen-p.yClose))}/>}</g>)}
          {axisIndices.map(i => <text className="candle-axis" key={i} x={layout.points[i].x} y={layout.height-10} textAnchor={i === 0 ? 'start' : i === bars.length-1 ? 'end' : 'middle'}>{candleAxisLabel(bars[i],interval,zone)}</text>)}
        </svg>
        <details className="candle-data"><summary>查看数值 · {bars.length} 根</summary><div className="table-wrap"><table><thead><tr><th>周期 · {timezoneLabel(zone)}</th><th className="right">开</th><th className="right">高</th><th className="right">低</th><th className="right">收</th><th className="right">观测点</th></tr></thead><tbody>{[...bars].reverse().map(b => <tr key={b.start}><td>{candleLabel(b,interval,zone)}{b.in_progress && ' · 形成中'}</td>{[b.open,b.high,b.low,b.close,b.sample_count].map((v,i) => <td className="right" key={i}>{i===4 ? v : money(v)}</td>)}</tr>)}</tbody></table></div></details>
      </>}
    <p className="candle-note">当前采集间隔：{sampling===3600 ? '每小时' : `${Math.round(sampling/60)} 分钟`} · 周期按 {timezoneLabel(zone)} · 净资产包含出入金影响</p>
    {bars.length > 0 && <p className="candle-note">高低值仅覆盖实际观测，缺失时段不补齐；只有一个点的 K 线显示为横线。</p>}
  </div>;
}
