import React, { useState } from 'react';
import { MonitoringPanel } from '../components/MonitoringPanel';
import { ServerPage } from '../components/ServerPage';
import { PreflightProgress } from '../components/PreflightProgress';
import { Button, Empty, Tag } from '../ui';
import { actions, statuses, time } from '../format';
import { isServerDeployment } from '../server';
import { attentionItems } from '../app/overview';

export function SystemPage({ view, state, busy, act, navigate, access, shutdown, refresh }) {
  const [includeObservations, setIncludeObservations] = useState(false);
  if (view === 'server') return <ServerPage onNavigate={navigate} onRuntimeChange={() => refresh().catch(() => {})}/>;
  if (view === 'monitoring') return <MonitoringPanel state={state} busy={busy} act={act} onNavigate={navigate}/>;
  const jobs = (state.jobs || []).filter(job => includeObservations || job.action !== 'observe');
  const attention = attentionItems(state);
  return <>
    {attention.length > 0 && <section className="overview-section"><div className="section-heading"><h2>当前需要处理</h2></div><div className="attention-list">{attention.map(item => <div key={item.id}><p>{item.message}</p><button className="text-link" onClick={() => navigate(item.destination)}>查看</button></div>)}</div></section>}
    <div className="section-toolbar">
      <div><h2>运行与日志</h2><p className="small muted">后台返回的最近 {state.jobs?.length || 0} 条操作；更早记录不在此列表。</p></div>
      <label className="inline-check"><input type="checkbox" checked={includeObservations} onChange={event => setIncludeObservations(event.target.checked)}/>显示账户定时观测</label>
    </div>
    <section className="job-list">
      {!jobs.length ? <Empty title="暂无此类操作记录">策略执行、连接检查和手动操作会显示在这里。</Empty> : jobs.map(job => <details className="job" key={job.id}>
        <summary><div><strong>{actions[job.action] || job.action}</strong><small>{time(job.started_at)} · {job.broker_name || job.broker || job.source_name || '后台操作'}</small></div><Tag tone={['blocked', 'paused', 'interrupted'].includes(job.status) ? 'amber' : 'muted'}>{statuses[job.status] || job.status}</Tag></summary>
        <div className="job-body"><p>{job.message}</p>
          {job.action === 'preflight' && <PreflightProgress job={job} onNavigate={navigate}/>}
          {job.result?.issues?.length > 0 && <ul>{job.result.issues.map((issue, index) => <li key={index}>{typeof issue === 'string' ? issue : issue.message || issue.code || '请检查配置与运行记录'}</li>)}</ul>}
          {job.events?.map((event, index) => <div className="log-line" key={index}><time>{time(event.at)}</time><span>{event.message}</span></div>)}
          {job.result?.diagnostic_id && <details className="details diagnostic-details"><summary>诊断信息 · {job.result.diagnostic_id}</summary><dl>
            {[['诊断编号', job.result.diagnostic_id], ['错误代码', job.result.error_code], ['执行阶段', job.result.phase], ['异常类型', job.result.exception_type]].filter(([, value]) => value).map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{value}</dd></div>)}
          </dl>{job.result.mismatched_fields?.length > 0 && <p className="small">不一致字段：{job.result.mismatched_fields.join('、')}</p>}{job.result.code_locations?.length > 0 && <ul>{job.result.code_locations.map((location, index) => <li key={index}><code>{location.file}:{location.line} · {location.function}</code></li>)}</ul>}</details>}
          <small className="muted">记录编号：{job.id}</small>
        </div>
      </details>)}
    </section>
    {!isServerDeployment(access) && <div className="system-exit"><p className="small muted">退出会关闭工作台和后台。仅停止策略，请前往策略运行。</p><Button disabled={busy} onClick={shutdown}>退出整个工作台</Button></div>}
  </>;
}
