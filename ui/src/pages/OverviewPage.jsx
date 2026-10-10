import React from 'react';
import { attentionItems, nextRun, runningRows } from '../app/overview';
import { Button, Tag } from '../ui';
import { actions, cls, relativeTime, time } from '../format';

// "现在": is the backend up, what runs next, and is anything waiting for me.
export function OverviewPage({ state, runtime, runtimeStatus, navigate }) {
  const rows = runningRows(state), attention = attentionItems(state);
  const enabled = rows.filter(item => item.enabled).length, next = nextRun(rows);
  const recent = (state.jobs || []).filter(job => job.action !== 'observe').slice(0, 4);
  return <div className="overview-page">
    <section className="status-cards" aria-label="运行摘要">
      <div className="status-card">
        <span>交易后台</span>
        <strong className={cls('status-value', runtimeStatus.tone)}><i aria-hidden="true"/>{runtimeStatus.label}</strong>
        <small>{runtime?.checked_at ? `${time(runtime.checked_at)} 已确认` : '正在检查后台'}</small>
      </div>
      <div className="status-card">
        <span>下次执行 · 本地时间</span>
        <strong className="status-value num">{next ? time(next) : '—'}</strong>
        <small>{next ? `${relativeTime(next)} · ${enabled} 个运行已开启` : enabled ? `${enabled} 个运行已开启，等待确认执行时间` : '没有开启的运行'}</small>
      </div>
      <div className={cls('status-card', attention.length > 0 && 'attention')}>
        <span>需要处理</span>
        <strong className="status-value">{attention.length ? `${attention.length} 项` : '没有'}</strong>
        <small>{attention.length ? attention[0].message : '一切正常，不需要你做什么。'}</small>
      </div>
    </section>
    {attention.length > 0 && <section className="card list-card" aria-label="需要处理">
      <div className="card-heading"><h2>需要处理</h2>{attention.length > 3 && <button className="text-link" onClick={() => navigate('logs')}>全部 {attention.length} 项<IconArrow/></button>}</div>
      {attention.slice(0, 3).map(item => <div className="list-row" key={item.id}>
        <i className="dot warning" aria-hidden="true"/>
        <p className="list-text">{item.message}</p>
        <Button className="sm" onClick={() => navigate(item.destination)}>去处理</Button>
      </div>)}
    </section>}
    <section className="card list-card" aria-label="运行中">
      <div className="card-heading"><h2>运行</h2>{rows.length > 0 && <button className="text-link" onClick={() => navigate('instances')}>全部运行<IconArrow/></button>}</div>
      {!rows.length ? <div className="empty-inline">
        <p>还没有运行。新建一个，选好策略、账户和计划就能开始。</p>
        <Button primary icon="plus" onClick={() => navigate({ page: 'runs', view: 'instances', create: true })}>新建运行</Button>
      </div> : rows.slice(0, 6).map(row => <div className="list-row" key={row.id}>
        <div className="list-main"><strong>{row.name}</strong><small>{row.account}</small></div>
        <Tag tone={row.attention ? 'amber' : row.enabled ? 'success' : 'muted'}>{row.status.label}</Tag>
        <span className="list-meta num">{row.next ? `下次 ${time(row.next)}` : row.enabled ? '等待确认时间' : '已停止'}</span>
        <button className="text-link" onClick={() => navigate(row.destination)}>详情<IconArrow/></button>
      </div>)}
    </section>
    {recent.length > 0 && <section className="card list-card" aria-label="最近记录">
      <div className="card-heading"><h2>最近记录</h2><button className="text-link" onClick={() => navigate('logs')}>全部记录<IconArrow/></button></div>
      {recent.map(job => <div className="list-row quiet" key={job.id}>
        <time className="num">{time(job.started_at)}</time>
        <span className="list-text">{actions[job.action] || job.action} · {job.message}</span>
      </div>)}
    </section>}
  </div>;
}

function IconArrow() {
  return <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true"><path d="M5 12h14m-5-5 5 5-5 5"/></svg>;
}
