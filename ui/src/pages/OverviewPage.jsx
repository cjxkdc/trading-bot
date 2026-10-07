import React from 'react';
import { attentionItems, nextRun, runningRows } from '../app/overview';
import { Button, Empty, Tag } from '../ui';
import { time } from '../format';

export function OverviewPage({ state, runtimeStatus, navigate }) {
  const rows = runningRows(state), attention = attentionItems(state);
  const enabled = rows.filter(item => item.enabled).length;
  return <div className="overview-page">
    <section className="overview-status" aria-label="运行摘要">
      <div><span>交易后台</span><strong>{runtimeStatus.label}</strong></div>
      <div><span>已开启的运行</span><strong>{enabled} 项</strong></div>
      <div><span>下次执行 · 本地时间</span><strong>{nextRun(rows) ? time(nextRun(rows)) : '暂无已确认时间'}</strong></div>
    </section>
    <section className="overview-section">
      <div className="section-heading"><h2>需要处理{attention.length > 0 && <span className="count">{attention.length}</span>}</h2>
        {attention.length > 3 && <button className="text-link" onClick={() => navigate('logs')}>查看全部</button>}
      </div>
      {!attention.length ? <p className="quiet-status">当前没有待处理事项。</p> : <div className="attention-list">
        {attention.slice(0, 3).map(item => <div key={item.id}><p>{item.message}</p><button className="text-link" onClick={() => navigate(item.destination)}>查看<IconArrow/></button></div>)}
      </div>}
    </section>
    <section className="overview-section">
      <div className="section-heading"><h2>策略运行</h2><Button primary onClick={() => navigate(rows.length ? 'instances' : { page: 'runs', view: 'instances', create: true })}>{rows.length ? '管理策略' : '创建策略实例'}</Button></div>
      {!rows.length ? <div className="overview-empty"><Empty title="还没有策略运行">创建实例后，在这里查看运行状态和下一次执行。</Empty><button className="text-link" onClick={() => navigate('manual')}>进入手动调仓</button></div> : <div className="run-summary-list">
        {rows.slice(0, 6).map(row => <div className="run-summary" key={row.id}>
          <div className="run-name"><strong>{row.name}</strong><small>{row.account}</small></div>
          <Tag tone={row.attention ? 'amber' : 'muted'}>{row.status.label}</Tag>
          <div className="run-next"><small>下次执行 · 本地时间</small><span>{row.next ? time(row.next) : row.enabled ? '等待确认' : '—'}</span></div>
          <button className="text-link" onClick={() => navigate(row.destination)}>查看<IconArrow/></button>
        </div>)}
        {rows.length > 6 && <button className="text-link list-more" onClick={() => navigate('instances')}>查看全部 {rows.length} 项运行</button>}
      </div>}
    </section>
  </div>;
}

function IconArrow() {
  return <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true"><path d="M5 12h14m-5-5 5 5-5 5"/></svg>;
}
