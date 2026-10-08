import React, { useEffect, useState } from 'react';
import { api } from '../api';
import { scheduleSummary } from '../automation';
import { instanceStatus, terminalOrderCount } from '../strategy_instances';
import { percent, relativeTime, time } from '../format';
import { Button, Empty, Tag } from '../ui';
import { InstanceForm } from './InstanceForm';
import { InstanceDetails } from './InstanceDetails';
import { Dialog } from './Dialog';

export function StrategyRuns({ state, busy, pending, act, route, onNavigate }) {
  const manager = state.strategy_manager || { instances: [], accounts: [], limits: {} };
  const items = manager.instances;
  const selected = items.find(item => item.id === route.instance);
  const terminal = selected ? terminalOrderCount(state.jobs, selected) : 0;
  const [editing, setEditing] = useState(route.create ? {} : null);
  const [confirm, setConfirm] = useState(null);
  const [limits, setLimits] = useState(manager.limits);
  useEffect(() => { setEditing(route.create ? {} : null); }, [route.instance, route.create, route.strategy]);
  const back = () => onNavigate('instances');
  async function confirmed() {
    const { item, action } = confirm;
    const result = await act(() => api(`strategy-instances/${item.id}/${action}`, action === 'remove' ? {} : { confirmation: item.id }));
    if (result) { setConfirm(null); if (action === 'remove') back(); }
  }
  function closeEditor() { setEditing(null); if (route.create) back(); }

  return <div className="runs-page">
    {manager.corrupt && <div className="alert error">运行状态无法核对，实例已停用；原文件保留供检查。</div>}
    {selected || route.instance ? <>
      <button className="text-link back-link" onClick={back}>返回策略实例</button>
      {!selected ? <Empty title="此实例已不存在">返回列表，查看当前保存的实例。</Empty> : <>
        <div className="instance-detail-heading"><div><h2>{selected.name}</h2><p>{moduleName(state.strategies, selected.strategy.type, 'type')} · {moduleName(state.brokers, selected.broker_id)}</p></div><Tag tone={instanceStatus(selected).tone}>{instanceStatus(selected).label}</Tag></div>
        <div className="instance-facts">
          <div><span>账户环境</span><strong>{state.brokers.find(b => b.id === selected.broker_id)?.mode_label || '环境待核验'}</strong></div>
          <div><span>账户资金比例</span><strong>{percent(selected.budget)}</strong></div>
          <div><span>运行计划</span><strong>{scheduleSummary(selected.schedule)}</strong></div>
          <div><span>下次执行 · 本地时间</span><strong>{selected.enabled ? time(selected.next_run) : '—'}</strong>{selected.enabled && selected.next_run && <small className="relative">{relativeTime(selected.next_run)}</small>}</div>
        </div>
        <p className={selected.pending || selected.status === 'paused' ? 'inline-warning' : 'small muted'} role="status">{selected.message}</p>
        {terminal > 0 && selected.pending && <p className="inline-warning">最近一次核对发现 {terminal} 笔委托已被券商撤销或拒绝。先在券商端确认原因；确认后停止此账户的实例，再用「关闭已撤单/拒单」结束本批次。</p>}
        <div className="button-row"><button className="text-link" onClick={() => onNavigate({ page: 'strategies', strategy: selected.strategy.type, tab: 'readme' })}>查看策略</button><button className="text-link" onClick={() => onNavigate({ page: 'strategies', strategy: selected.strategy.type, tab: 'research' })}>查看回测实验</button></div>
        <div className="instance-action-bar">
          {selected.pending && <Button primary disabled={busy} onClick={() => act(() => api(`strategy-instances/${selected.id}/reconcile`, {}))}>核对订单与归属</Button>}
          {selected.enabled ? <Button disabled={pending} onClick={() => act(() => api(`strategy-instances/${selected.id}/stop`, {}))}>停止实例</Button> : <Button primary={!selected.pending} disabled={busy || selected.pending || manager.corrupt} onClick={() => setConfirm({ item: selected, action: 'start' })}>启动实例</Button>}
          <Button disabled={busy || selected.pending} onClick={() => setConfirm({ item: selected, action: 'once' })}>执行一轮</Button>
          <Button disabled={busy || selected.pending || manager.corrupt} title={selected.pending ? '先核对未完成批次' : '使用已有数据，休市时也可预览'} onClick={() => act(() => api(`strategy-instances/${selected.id}/preview`, {}))}>预览选股</Button>
          <Button disabled={busy || selected.enabled || selected.pending} onClick={() => setEditing(selected)}>修改设置</Button>
        </div>
        {(selected.pending || selected.enabled || manager.corrupt) && <p className="small muted">{selected.pending ? '先核对未完成批次，再执行或修改设置。停止不会撤销已提交委托。' : manager.corrupt ? '请先检查保存的实例文件。' : '已开启的实例需先停止，才能修改设置。'}</p>}
        {editing && <InstanceForm key={selected.id} state={state} item={selected} busy={busy} act={act} close={closeEditor}/>}
        <InstanceDetails state={state} item={selected} busy={busy} act={act}/>
        <details className="details instance-maintenance"><summary>连接检查与维护</summary>
          <p className="small muted">数据源：{moduleName(state.sources, selected.source_id)}。结束余单会保留已成交持仓，不撤销券商订单。</p>
          <div className="button-row"><Button disabled={busy} onClick={() => act(() => api(`strategy-instances/${selected.id}/check`, {}))}>检查连接</Button>
            {selected.pending && !selected.enabled && <Button disabled={busy} onClick={() => setConfirm({ item: selected, action: 'resolve' })}>结束未提交余单</Button>}
            {selected.pending && !selected.enabled && <Button className={terminal > 0 ? 'danger' : ''} disabled={busy} title="仅适用于券商已确认撤销或拒绝的委托；状态不明、等待成交或部分成交仍需先核对。" onClick={() => setConfirm({ item: selected, action: 'close-terminal' })}>关闭已撤单/拒单</Button>}
            {!selected.enabled && !selected.positions.length && <Button disabled={busy || selected.pending} onClick={() => setConfirm({ item: selected, action: 'remove' })}>删除实例</Button>}
          </div>
        </details>
      </>}
    </> : <>
      <div className="section-toolbar"><p className="page-intro">每份实例独立配置与运行，共用账户时合并目标下单。</p><Button primary disabled={busy || manager.corrupt} onClick={() => onNavigate({ page: 'runs', view: 'instances', create: true })}>创建实例</Button></div>
      {editing && (route.strategy && !state.strategies.some(item => item.type === route.strategy && item.enabled) ? <Empty title="所选策略已不可用">返回策略工作区重新选择，再创建运行实例。</Empty> : <InstanceForm key={`new-${route.strategy || "default"}`} state={state} item={null} initialStrategy={route.strategy} busy={busy} act={act} close={closeEditor}/>)}
      {!items.length && !editing ? <Empty title="创建第一份策略实例">选择策略、账户资金比例和调仓计划。保存后再确认启动。</Empty> : <div className="instance-list">
        {items.map(item => <article className="instance-row" key={item.id}>
          <div className="instance-row-name"><h3>{item.name}</h3><small>{moduleName(state.strategies, item.strategy.type, 'type')} · {moduleName(state.brokers, item.broker_id)}</small></div>
          <div className="instance-row-budget"><small>资金比例</small><span>{percent(item.budget)}</span><span className="budget-bar" aria-hidden="true"><i style={{ width: `${Math.min(100, Number(item.budget) * 100)}%` }}/></span></div>
          <div className="instance-row-holdings"><small>持仓</small><span>{item.positions?.length || 0} 只</span></div>
          <div className="instance-row-next"><small>下次执行 · 本地时间</small><span>{item.enabled && item.next_run ? <>{time(item.next_run)} <em>{relativeTime(item.next_run)}</em></> : '—'}</span></div>
          <Tag tone={item.pending || item.status === 'paused' ? 'amber' : 'muted'}>{instanceStatus(item).label}</Tag>
          <Button onClick={() => onNavigate({ page: 'runs', view: 'instances', instance: item.id })}>查看详情</Button>
        </article>)}
      </div>}
      {manager.accounts?.length > 0 && <details className="details account-allocation"><summary>账户资金比例</summary>
        {manager.accounts.map(account => <div className="allocation-row" key={account.broker_id}>
          <strong>{moduleName(state.brokers, account.broker_id)}</strong><span>已预留 {percent(account.reserved_budget)}</span><span>可分配 {percent(account.available_budget)}</span>
          <small>{account.running_count} 份已开启 · {account.stopped_holding_count} 份停止并保留持仓{account.pending ? ' · 有待核对批次' : ''}</small>
        </div>)}<p className="small muted">比例是账户净值的上限。无持仓的停止实例不预留资金。</p>
      </details>}
      {Object.values(manager.unmanaged || {}).some(positions => Object.keys(positions).length) && <details className="details"><summary>账户中保留的其他持仓</summary>{Object.entries(manager.unmanaged).map(([broker, positions]) => <p key={broker}>{moduleName(state.brokers, broker)}：{Object.entries(positions).map(([symbol, quantity]) => `${symbol} ${quantity} 股`).join('，')}</p>)}</details>}
      <details className="details"><summary>计算与日志限制</summary><form className="instance-export" onSubmit={event => { event.preventDefault(); act(() => api('strategy-instances/limits', limits)); }}>
        <label>每次计算上限（秒）<input type="number" min="10" max="120" value={limits.selection_timeout_seconds || 60} onChange={event => setLimits({ ...limits, selection_timeout_seconds: Number(event.target.value) })}/></label>
        <label>保留执行记录（轮）<input type="number" min="20" max="500" value={limits.log_retention_runs || 100} onChange={event => setLimits({ ...limits, log_retention_runs: Number(event.target.value) })}/></label>
        <Button type="submit" disabled={busy || manager.running_count > 0 || items.some(item => item.pending)}>保存限制</Button>
      </form><p className="small muted">修改前停止所有实例并核对未完成批次。策略计算超时不会强制终止下单进程。</p></details>
    </>}
    {confirm && <Dialog labelledBy="instance-confirm-title" onDismiss={() => setConfirm(null)}>
      <h2 id="instance-confirm-title">{confirm.action === 'remove' ? '删除策略实例' : confirm.action === 'resolve' ? '结束未提交余单' : confirm.action === 'close-terminal' ? '关闭已撤单/拒单' : confirm.action === 'once' ? '执行一轮模拟交易' : '启动策略实例'}</h2>
      <p>{confirm.item.name} · 资金比例 {percent(confirm.item.budget)}。</p>
      <p>{confirm.action === 'remove' ? '删除此实例，历史记录保留。' : confirm.action === 'resolve' ? '核对已提交订单后，结束此账户批次中尚未提交的委托，按实际成交更新持仓归属。保留已成交持仓；本操作不提交新订单，也不撤销已提交订单。' : confirm.action === 'close-terminal' ? '重新向券商查询后，把券商确认已撤销或已拒绝的委托标记为「已关闭」，同批次未提交的计划一并结束，并写入审计记录。部分成交的股数按实际持仓保留；不会补单或撤单。存在状态不明、等待成交或部分成交的委托时会被拒绝。' : confirm.action === 'once' ? '先检查账户与数据，进入可用交易窗口后执行一轮；原先停止的实例完成后继续停止。' : '先检查账户与数据，再按自己的计划进行模拟建仓和调仓。关闭网页后后台继续运行。'}</p>
      {confirm.action === 'close-terminal' && <label className="review-check"><input type="checkbox" checked={confirm.checked === true} onChange={e => setConfirm({ ...confirm, checked: e.target.checked })}/>我已在券商端核对这些撤单或拒单的原因。</label>}
      <div className="button-row"><Button autoFocus onClick={() => setConfirm(null)}>取消</Button><Button primary disabled={busy || (confirm.action === 'close-terminal' && confirm.checked !== true)} onClick={confirmed}>确认</Button></div>
    </Dialog>}
  </div>;
}

function moduleName(items, id, key = 'id') { return items?.find(item => item[key] === id)?.name || id; }
