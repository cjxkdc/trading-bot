import React, { useEffect, useRef, useState } from 'react';
import { api, downloadCode } from '../api';
import { Button, Empty, Tag, Icon } from '../ui';
import { cls, percent, number, time } from '../format';
import { scheduleSummary } from '../automation';
import { orderSide, orderStatus } from '../orders';
import { ModuleFields } from '../components/ModuleFields';
import { ModuleIssues } from '../components/ModuleIssues';

export function DataPage({ state, busy, act, run, chooseSource, upload, selectedFile }) {
  const source = state.sources.find(s => s.id === state.source), snap = state.snapshot;
  const can = capability => source.capabilities?.includes(capability);
  const [settings, setSettings] = useState(() => Object.fromEntries((source.fields || []).map(f => [f.name, f.secret ? '' : source[f.name] ?? f.default ?? ''])));
  async function save(e) { e.preventDefault(); const values = { ...settings }; setSettings(Object.fromEntries(Object.entries(settings).map(([key, value]) => [key, source.fields.find(f => f.name === key)?.secret ? '' : value]))); await act(() => { for (const field of source.fields || []) if (field.type === 'json' && typeof values[field.name] === 'string') values[field.name] = JSON.parse(values[field.name]); return api('data/settings', values); }); }
  return <><section className="card"><div className="card-heading"><div><h2>数据模块</h2><p>连接设置由数据模块提供。下方快照与预览对应手动配置，实例保留各自的数据源选择。</p></div><Tag>{source.name}</Tag></div><div className="data-source-row"><label>手动调仓数据源<select aria-label="手动调仓数据源" value={state.source} disabled={busy} onChange={e => chooseSource(e.target.value)}>{state.sources.map(s => <option key={s.id} value={s.id} disabled={!s.enabled} title={s.reason}>{s.name}{!s.enabled && ' · 不可用'}</option>)}</select></label><div><strong>{source.name}</strong><p className="muted small">{source.message || source.description}</p></div></div>{source.issue && <p className="inline-warning">{source.issue}</p>}{state.compatibility_issue && <p className="inline-warning">{state.compatibility_issue}</p>}
    {state.data_requirements && <div className="info-grid"><div><span>手动配置的数据范围</span><strong>{state.data_requirements.universe_label}</strong></div><div><span>日线历史</span><strong>{state.data_requirements.price_bars} 个交易日</strong></div><div><span>按需读取字段</span><strong>{state.data_requirements.field_labels.join('、')}</strong></div></div>}
    {can('configure') && <details className="settings" open={source.configured === false}><summary>{source.name} · 连接设置</summary><form onSubmit={save}><div className="form-grid"><ModuleFields fields={source.fields || []} values={settings} disabled={busy} onChange={(key, value) => setSettings({ ...settings, [key]: value })}/></div><div className="card-actions"><Button type="submit" disabled={busy}>保存设置</Button><span className="small muted">密钥只保存在本机，不返回页面。</span></div></form></details>}
    <div className="card-actions">{can('collect') && <Button primary icon="refresh" disabled={busy || !state.actions?.refresh?.enabled} title={state.actions?.refresh?.message} onClick={() => run('refresh')}>下载并预览</Button>}{can('inspect') && <Button disabled={busy || !state.actions?.['data-status']?.enabled} title={state.actions?.['data-status']?.message} onClick={() => run('data-status')}>检查数据权限</Button>}{can('upload') && <Button icon="upload" disabled={busy} onClick={() => selectedFile.current?.click()}>导入快照</Button>}<input type="file" accept=".json,application/json" ref={selectedFile} hidden onChange={e => { upload(e.target.files?.[0]); e.target.value = ''; }}/></div></section>
    <section className="card"><div className="card-heading"><h2>当前快照</h2>{snap && <Tag tone="blue">{snap.research_only ? '仅供研究' : snap.synthetic ? '合成演示' : '待执行校验'}</Tag>}</div>{!snap ? <Empty title="这个数据模块还没有快照" icon="database">通过模块支持的下载或文件输入方式准备数据。</Empty> : <><div className="info-grid"><div><span>价格日期</span><strong>{snap.price_as_of || '—'}</strong></div><div><span>有效 / 候选股票</span><strong>{number(snap.usable_count)} / {number(snap.candidate_count)}</strong></div><div><span>采集完成时间</span><strong>{time(snap.collected_at)}</strong></div></div><p className="small muted">{snap.collection_complete === false ? '采集未完整覆盖候选股票；请结合缺失范围判断选股结果。' : snap.collection_complete === true ? '采集流程已完成，仍需检查数据说明。' : '文件未提供完整采集状态。'}</p>{snap.limitations?.length > 0 && <details className="details"><summary>数据说明与限制</summary><ul>{snap.limitations.map((item, i) => <li key={i}>{typeof item === 'string' ? item : JSON.stringify(item)}</li>)}</ul></details>}<Button disabled={busy || !state.actions?.preview?.enabled} title={state.actions?.preview?.message} onClick={() => run('preview')}>用此快照预览策略</Button></>}</section>
    <ModuleIssues items={state.sources}/><details className="details"><summary>所有已登记的数据接口</summary><ul>{state.inventory.data.map(item => <li key={item.type}>{item.name} · {item.kind}{!item.has_control && ' · 尚未接入工作台控制接口'}</li>)}</ul></details></>;
}

export function BrokerPage({ state, busy, run, chooseBroker, authorize, bind, disconnect }) {
  const broker = state.broker, account = state.account, auth = state.broker_authorization;
  const loginMode = broker.authentication?.mode;
  return <>
    <section className="card">
      <div className="card-heading"><div><h2>当前账户模块</h2><p>切换后重新核验账户，旧订单预览失效。</p></div><Tag tone={state.broker_verified ? 'blue' : 'amber'}>{state.broker_verified ? '已核验' : '待核验'}</Tag></div>
      <div className="data-source-row"><label>券商模块<select aria-label="券商模块" value={broker.id} disabled={busy} onChange={e => chooseBroker(e.target.value)}>{state.brokers.map(item => <option key={item.id} value={item.id} disabled={!item.enabled}>{item.name}{!item.enabled && ' · 未启用'}</option>)}</select></label><div><strong>{broker.name}</strong><p className="small muted">{broker.description}</p></div></div>
      {loginMode === 'oauth' ? <div className="workflow authorization-panel">
        <div><strong>{broker.authentication.title}</strong><p className="small muted">{auth?.notice || auth?.note || '通过券商官网完成授权，密码不交给本程序。'}</p></div>
        <Button disabled={busy} onClick={authorize}>{auth?.authorized ? '重新网页授权' : '前往券商官网授权'}</Button>
        {auth?.authorized && <label>美股模拟账户<select aria-label="网页模拟账户" disabled={busy} value={auth.selected || ''} onChange={e => e.target.value && bind(e.target.value)}><option value="" disabled>请选择账户</option>{auth.accounts.map(item => <option key={item.id} value={item.id}>{item.name} · {item.account_hint}</option>)}</select></label>}
      </div> : <p className="small muted">{broker.authentication?.title}。{loginMode === 'external' ? '请先在官方客户端或当前模块的 API 配置中登录，然后检查账户连接。' : '使用本地回放账户。'}</p>}
      <div className="info-grid"><div><span>账户环境</span><strong>{broker.mode_label}</strong></div><div><span>模块版本</span><strong>{broker.version}</strong></div><div><span>最近检查</span><strong>{time(account?.checked_at)}</strong></div></div>
      <div className="card-actions">{broker.capabilities.includes('inspect') && <><Button primary disabled={busy || !state.actions?.status?.enabled} title={state.actions?.status?.message} onClick={() => run('status')}>检查账户连接</Button><Button disabled={busy || !state.actions?.offline?.enabled} onClick={() => run('offline')}>检查本地配置</Button></>}<Button disabled={busy} onClick={disconnect}>断开当前账户</Button></div>
      {account && <><p className="result-message">{account.message}</p>{account.details?.issues?.length > 0 && <ul className="small muted">{account.details.issues.map((item, i) => <li key={i}>{typeof item === 'string' ? item : JSON.stringify(item)}</li>)}</ul>}</>}
    </section>
    <ModuleIssues items={state.brokers}/><section className="card quiet-card"><h2>独立的账户状态</h2><p>当前程序只核验所选模块。切换时清除网页授权和旧账户预览；仍有待核对委托时先处理订单。</p><p>断开仅停用本程序的账户访问。官方客户端登录或券商端授权，需要在对应平台退出或撤销。当前工作台仅开放模拟与本地回放。</p><details className="details"><summary>所有已登记的账户接口</summary><ul>{state.inventory.broker.map(item => <li key={item.type}>{item.name} · {item.kind}{!item.has_control && ' · 尚未接入工作台控制接口'}</li>)}</ul></details></section>
  </>;
}
