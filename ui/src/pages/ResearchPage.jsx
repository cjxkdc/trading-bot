import React, { useEffect, useRef, useState } from 'react';
import { api, downloadCode } from '../api';
import { Button, Empty, Tag } from '../ui';
import { percent, number } from '../format';
import { scheduleSummary } from '../automation';
import { researchSources, resolveResearchSource, reconcileResearchSelection, researchExperiments, researchResultSelection } from '../research_context';

export function ResearchPage({ state, strategyType, busy, act, navigate }) {
  const [selection, setSelection] = useState(() => ({ strategyType, sourceId: '' }));
  const [sourceChanged, setSourceChanged] = useState(false);
  const sources = researchSources(state, strategyType);
  const source = resolveResearchSource(state, strategyType, selection);
  const cfg = state.backtest_config || {};
  const [start, setStart] = useState(cfg.start || ''), [end, setEnd] = useState(cfg.end || '');
  const [slip, setSlip] = useState(cfg.slippage_bps ?? 10);
  const [items, setItems] = useState([]), [loadError, setLoadError] = useState('');
  const [selected, setSelected] = useState([]), [comparison, setComparison] = useState(null);
  const [note, setNote] = useState(''), [importTarget, setImportTarget] = useState(null);
  const input = useRef(null);
  const { matching: experiments, unassigned } = researchExperiments(items, strategyType);
  const resultIds = researchResultSelection(experiments, selected);
  const shownComparison = comparison && comparison.strategyType === strategyType &&
    researchResultSelection(experiments, comparison.ids).length === comparison.ids.length ? comparison.result : null;

  async function load() {
    try { setItems((await api('experiments')).items); setLoadError(''); }
    catch (error) { setLoadError(error.message); }
  }
  useEffect(() => { load(); }, []);
  useEffect(() => {
    const next = reconcileResearchSelection(state, strategyType, selection);
    if (next !== selection) {
      setSourceChanged(!!selection.sourceId && selection.strategyType === strategyType);
      setSelection(next);
    }
  }, [state, strategyType, selection]);

  async function exportCode(event) {
    event.preventDefault();
    const context = resolveResearchSource(state, strategyType, selection);
    if (!context) { setSourceChanged(true); return; }
    const result = await act(() => api(context.kind === 'instance' ? `strategy-instances/${context.instance.id}/export` : 'experiments', {
      start, end, slippage_bps: slip, expected_strategy_type: strategyType,
      ...(context.kind === 'manual' ? { note } : {}),
    }));
    if (result) {
      await load();
      await act(() => downloadCode(result.id), '代码已准备下载。复制到 QuantConnect 后运行回测。');
    }
  }

  async function importFile(file) {
    const target = importTarget;
    setImportTarget(null);
    if (!file || target?.strategyType !== strategyType || !experiments.some(item => item.id === target.id)) return;
    const result = await act(() => api(`experiments/${target.id}/result`, file, { raw: true }));
    if (result) await load();
  }

  async function compare() {
    const ids = researchResultSelection(experiments, selected);
    if (ids.length < 2) return;
    const result = await act(() => api('compare', { ids }));
    if (result) setComparison({ strategyType, ids, result });
  }

  const completions = { reported_complete: '报告显示完成', unverified: '完成状态未核验', incomplete: '未完成', failed: '运行失败' };
  return <>
    <section className="card">
      <div className="card-heading"><div><h2>创建回测实验</h2><p>按此策略的参数与计划导出代码，在 QuantConnect 运行回测。</p></div><Tag>QuantConnect</Tag></div>
      {!sources.length ? <>
        <Empty title="先为此策略保存一份配置" icon="chart">保存参数，或创建运行实例后，即可导出这套策略的回测代码。</Empty>
        <div className="button-row">
          <Button primary disabled={busy || !strategyType} onClick={() => navigate({ page: 'strategies', strategy: strategyType, tab: 'parameters' })}>配置参数</Button>
          <Button disabled={busy || !strategyType} onClick={() => navigate({ page: 'strategies', strategy: strategyType, tab: 'runs' })}>运行实例</Button>
        </div>
      </> : <>
        <label className="research-source">参数与计划来源
          <select value={source ? selection.sourceId : ''} disabled={busy} onChange={event => {
            setSelection({ strategyType, sourceId: event.target.value }); setSourceChanged(false);
          }}>
            <option value="">请选择此策略的一份配置</option>
            {sources.map(item => <option key={item.id} value={item.id}>{item.label}</option>)}
          </select>
        </label>
        {sourceChanged && <p className="inline-warning" role="status">原来源已改变或移除，请重新选择此策略的配置。</p>}
        {source && <p className="small muted">调仓：{scheduleSummary(source.schedule)}；首次建仓：{source.schedule?.initial_entry === 'scheduled' ? '等待计划日' : '首个可用交易日'}。</p>}
        <details className="details"><summary>回测与运行的差异</summary><p className="small muted">QC 使用历史股票池及基本面、市场单成交模型和设定的手续费／滑点；运行使用所选数据源与券商 DAY 限价单。调仓规则一致不等于成交价格一致。回测首次建仓从首个可用交易日开始，程序中途开启则按当时可用时段开始。</p></details>
        <form onSubmit={exportCode}>
          <div className="backtest-form">
            <label>开始日期<input type="date" required value={start} disabled={busy || !source} onChange={event => setStart(event.target.value)}/></label>
            <label>结束日期<input type="date" required value={end} disabled={busy || !source} onChange={event => setEnd(event.target.value)}/></label>
            <label>滑点 (bps)<input type="number" min="0" max="9999" required value={slip} disabled={busy || !source} onChange={event => setSlip(Number(event.target.value))}/></label>
            <Button primary type="submit" disabled={busy || !source}>导出回测代码</Button>
          </div>
          {source?.kind === 'manual' && <details className="details"><summary>实验备注</summary><textarea value={note} disabled={busy} onChange={event => setNote(event.target.value)} maxLength={2000} placeholder="这次想验证什么？"/></details>}
        </form>
      </>}
    </section>

    <div className="section-toolbar"><h2>此策略的实验记录</h2><Button disabled={busy || resultIds.length < 2} onClick={compare}>比较已选结果 ({resultIds.length})</Button></div>
    {loadError && <div className="alert error">{loadError}</div>}
    {!experiments.length ? <section className="card"><Empty title="此策略还没有可核验的实验记录" icon="chart">导出代码后，这里会保留冻结的参数、计划与代码。已有命令行实验继续保存在原目录。</Empty></section> : experiments.map(item => <section className="card experiment" key={item.id}>
      <div className="card-heading">
        <div><h2>{item.configuration.backtest.start} — {item.configuration.backtest.end}</h2><p>{item.id} · 滑点 {item.configuration.backtest.slippage_bps} bps</p></div>
        <div className="button-row">
          <Button disabled={busy} onClick={() => act(() => downloadCode(item.id), '代码已准备下载。')}>下载冻结代码</Button>
          <Button disabled={busy} onClick={() => { setImportTarget({ strategyType, id: item.id }); input.current?.click(); }}>导入结果 JSON</Button>
        </div>
      </div>
      <p className="small muted">冻结计划：{scheduleSummary(item.configuration.schedule)} · {item.configuration.schedule?.initial_entry === 'immediate' ? '首个可用交易日建仓' : '计划日建仓'}</p>
      {item.note && <p className="small muted">{item.note}</p>}
      {!item.results.length ? <p className="small muted">仅导出，尚无回测结果。</p> : item.results.map(result => result.invalid ? <p key={result.id} className="inline-warning">结果证据校验失败，已禁止比较。</p> : <div className="backtest-result" key={result.id}>
        <label className="result-select"><input type="checkbox" checked={resultIds.includes(result.id)} disabled={busy} onChange={event => setSelected(event.target.checked ? [...resultIds, result.id] : resultIds.filter(id => id !== result.id))}/><span>{completions[result.completion]} · {result.code_evidence === 'source_unverified' ? '代码来源未核验' : '代码副本匹配'}</span></label>
        <div className="result-metrics">
          <div><span>总收益</span><strong>{percent(result.metrics.total_return)}</strong></div>
          <div><span>年化收益</span><strong>{percent(result.metrics.annual_return)}</strong></div>
          <div><span>最大回撤</span><strong>{percent(result.metrics.max_drawdown)}</strong></div>
          <div><span>Sharpe</span><strong>{number(result.metrics.sharpe)}</strong></div>
          <div><span>订单数</span><strong>{number(result.metrics.total_orders)}</strong></div>
        </div>
      </div>)}
    </section>)}
    {!!unassigned.length && <details className="details"><summary>无法归属的记录 ({unassigned.length})</summary><p className="small muted">工作台中这些记录校验失败或缺少策略标识，未计入此策略的实验，也不能参与比较。</p><ul>{unassigned.map((item, index) => <li key={item?.id || index}>{item?.id || '未知记录'}</li>)}</ul></details>}
    <input hidden ref={input} type="file" accept=".json,application/json" onChange={event => { importFile(event.target.files?.[0]); event.target.value = ''; }}/>
    {shownComparison && <section className="card">
      <div className="card-heading"><div><h2>结果比较</h2><p>{shownComparison.comparison_differences.length ? `测试条件存在差异：${shownComparison.comparison_differences.join('、')}` : '区间、初始资金和成本设置相同。'} 比较不会自动批准实盘。</p></div></div>
      <div className="table-wrap"><table><thead><tr><th>实验</th><th>总收益</th><th>年化收益</th><th>回撤</th><th>Sharpe</th></tr></thead><tbody>{shownComparison.rows.map((row, index) => <tr key={index}><td>{row.experiment}</td><td>{percent(row.total_return)}</td><td>{percent(row.annual_return)}</td><td>{percent(row.max_drawdown)}</td><td>{number(row.sharpe)}</td></tr>)}</tbody></table></div>
    </section>}
  </>;
}
