import React from 'react';
import { Empty } from '../ui';
import { ResearchPage } from './ResearchPage';

// "回测": one strategy at a time; the experiments and comparisons live here.
export function BacktestsPage({ state, busy, act, route, navigate }) {
  const strategies = state.strategies;
  const selected = route.strategy ? strategies.find(item => item.type === route.strategy)
    : strategies.find(item => item.type === state.strategy.type) || strategies[0];
  if (!selected) return <Empty title="还没有策略" icon="chart">先在「策略」导入一个策略包，再回来跑回测。</Empty>;
  return <div className="backtests-page">
    <div className="context-toolbar">
      <label>策略<select value={selected.type} disabled={busy} onChange={event => navigate({ page: 'backtests', strategy: event.target.value })}>
        {strategies.map(item => <option key={item.type} value={item.type} disabled={!item.enabled}>{item.name}{!item.enabled && ' · 暂不可用'}</option>)}
      </select></label>
      <div><span className="small muted">v{selected.version} · {selected.type}</span><button className="text-link" onClick={() => navigate({ page: 'strategies', strategy: selected.type, tab: 'code' })}>查看代码 →</button></div>
    </div>
    <ResearchPage key={selected.type} strategyType={selected.type} state={state} busy={busy} act={act} navigate={navigate}/>
  </div>;
}
