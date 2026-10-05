import React, { useEffect, useState } from 'react';
import { api } from '../api';
const time = value => value ? new Date(value).toLocaleString('zh-CN',{hour12:false}) : '尚无记录';
const deliveries={local:'仅面板',pending:'待发送',sending:'发送中',delivered:'已送达',failed:'发送失败',cancelled:'已取消'};

export function MonitoringPanel({ state,busy,act,onNavigate,compact=false }) {
  const monitor=state.monitoring || {}, settings=monitor.settings || {};
  const [draft,setDraft]=useState({enabled:true,interval_seconds:900,channel:'none'});
  const [secrets,setSecrets]=useState({heartbeat_url:''});
  const channel=(settings.channels || []).find(c=>c.id===draft.channel);
  const saved=JSON.stringify(settings);
  useEffect(()=>{setDraft({enabled:settings.enabled ?? true,interval_seconds:settings.interval_seconds || 900,channel:settings.channel || 'none'});},[saved]);
  const events=monitor.events || [];
  async function save(e) {e.preventDefault();const r=await act(()=>api('monitoring/config',{...draft,...secrets}));if(r)setSecrets({heartbeat_url:''});}
  return <section className="card monitoring-panel"><div className="card-heading"><div><h2>{compact ? '最近通知' : '监控与通知'}</h2><p>{settings.enabled ? `账户每 ${Math.round((settings.interval_seconds || 900)/60)} 分钟检查，自动调仓关闭后仍继续。` : '账户定时监控已关闭，事件记录与通知仍可使用。'}</p></div>{compact && <button className="text-link" onClick={()=>onNavigate('logs')}>设置</button>}</div>
    {settings.configuration_error && <p className="inline-warning">监控配置无法读取，请重新保存设置。</p>}
    {!compact && <form onSubmit={save}><div className="monitor-form"><label>账户定时监控<select value={draft.enabled ? 'on' : 'off'} onChange={e=>setDraft({...draft,enabled:e.target.value==='on'})}><option value="on">开启</option><option value="off">关闭</option></select></label><label>检查间隔<select value={draft.interval_seconds} onChange={e=>setDraft({...draft,interval_seconds:Number(e.target.value)})}>{[300,900,1800,3600].map(t=><option key={t} value={t}>{t/60} 分钟</option>)}</select></label><label>通知渠道<select value={draft.channel} onChange={e=>setDraft({...draft,channel:e.target.value})}><option value="none">仅工作台</option>{settings.channels?.map(c=><option key={c.id} value={c.id}>{c.name}</option>)}</select></label></div>
      {channel && <div className="monitor-form">{channel.fields?.map(field=><label key={field.name}>{field.label}<input type={field.secret ? 'password' : 'text'} autoComplete="new-password" value={secrets[field.name] || ''} placeholder={channel.configured ? '已保存；留空保留' : field.label} onChange={e=>setSecrets({...secrets,[field.name]:e.target.value})}/></label>)}</div>}
      <details className="details"><summary>整台服务器掉线提醒 · {settings.heartbeat_configured ? '已配置外部心跳' : '未配置'}</summary><p className="small muted">在 Healthchecks 创建检查，周期设为 1 分钟、宽限 5 分钟，并在那里设置提醒渠道。这里每分钟发送心跳，服务器失联由外部服务提醒。</p><label>Healthchecks 心跳地址<input type="password" autoComplete="off" value={secrets.heartbeat_url} placeholder={settings.heartbeat_configured ? '已保存；留空保留' : 'https://hc-ping.com/…'} onChange={e=>setSecrets({...secrets,heartbeat_url:e.target.value})}/></label>{settings.heartbeat_configured && <button className="text-link" type="button" disabled={busy} onClick={()=>act(()=>api('monitoring/config',{clear_heartbeat:true}))}>移除心跳配置</button>}</details>
      <div className="button-row"><button className="button primary" disabled={busy}>保存监控设置</button><button className="button" type="button" disabled={busy || settings.channel==='none'} onClick={()=>act(()=>api('monitoring/test',{}))}>发送测试通知</button></div><p className="small muted">通知只发送操作结果和故障摘要，不包含账户号码、密钥或持仓。重试最多 3 次；网络超时可能产生重复消息，可按事件编号核对。</p></form>}
    {!events.length ? <p className="small muted">暂无事件。</p> : <div className="monitor-events">{events.slice(0,compact ? 2 : 12).map(e=><div key={e.id} className="monitor-event"><span className={'tag '+(e.severity==='error'||e.severity==='warning' ? 'amber' : 'muted')}>{deliveries[e.delivery] || e.delivery}</span><div><p>{e.message}</p><small className="muted">{time(e.at)}</small></div>{e.next_action && <button className="text-link" onClick={()=>onNavigate(e.next_action)}>查看</button>}</div>)}</div>}
  </section>;
}
