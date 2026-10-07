import React, { useEffect, useRef, useState } from 'react';
import { api, downloadCode } from '../api';
import { Button, Empty, Tag, Icon } from '../ui';
import { cls, percent, number, time } from '../format';
import { scheduleSummary } from '../automation';
import { orderSide, orderStatus } from '../orders';
import { preflightControls } from '../preflight';
import { PreflightProgress } from '../components/PreflightProgress';
import { TargetTable } from '../ui';
export function ManualRun({ state, busy, act, run, navigate, chooseSource, submit }) {
 const account = state.account, connected = account?.status === 'connected';
 const accountDetails = connected ? account.details : null;
 const preview = state.preview, snapshot = state.snapshot;
 const selectedData = state.sources.find(s => s.id === state.source);
 const dataCan = capability => selectedData?.capabilities?.includes(capability);
 const allowed = action => state.actions?.[action]?.enabled === true;
 const preflight = preflightControls(state, busy);
 return <>{state.managed_account && <p className="inline-warning">此账户由策略实例管理，手动下单不可用。请在策略实例中操作，或选择其他账户。</p>}
<div className="summary-row"><div><span className="metric-label">当前策略</span><strong>{state.strategy_name}</strong><button className="text-link" onClick={() => navigate('strategies')}>查看规则 <Icon name="arrow" size={14}/></button></div><div><span className="metric-label">数据日期 · {state.sources.find(s => s.id === state.source)?.name}</span><strong>{snapshot?.price_as_of || '暂无快照'}</strong><small>{snapshot ? `${snapshot.usable_count} 只有效 / ${snapshot.candidate_count} 只候选` : '下载或导入后开始预览'}</small></div><div><span className="metric-label">下单可用现金 · {state.broker.mode_label}</span><strong className="mono">{connected ? `${accountDetails.currency || ''} ${number(accountDetails.cash)}` : '—'}</strong><small>{connected ? `检查于 ${time(account.checked_at)}` : '尚未检查账户连接'}</small></div></div>
            <div className="home-grid"><section className="card main-card"><div className="card-heading"><div><h2>本次选股</h2><p>{preview ? `${state.strategy_name} · ${time(preview.created_at)}` : '用当前策略生成目标组合'}</p></div><Tag tone={preview ? 'blue' : 'muted'}>{preview ? preview.execution_ready ? '预览已就绪' : '研究预览' : '等待预览'}</Tag></div>
              <div className="workflow preflight-workflow"><label>数据源<select aria-label="手动调仓数据源" value={state.source} disabled={busy} onChange={e => chooseSource(e.target.value)}>{state.sources.map(s => <option key={s.id} value={s.id} disabled={!s.enabled} title={s.reason}>{s.name}{!s.enabled && ' · 不可用'}</option>)}</select></label><div className="preflight-start"><Button primary disabled={!preflight.canTest} title={state.actions?.preflight?.message} icon="check" onClick={() => act(() => api('jobs', preflight.request))}>{state.active?.action === 'preflight' ? '正在测试…' : '一键测试并准备'}</Button></div></div>
              <details className="preflight-advanced"><summary>分步操作</summary><div className="button-row">{dataCan('collect') && <Button disabled={busy || !allowed('refresh')} title={state.actions?.refresh?.message} icon="refresh" onClick={() => run('refresh')}>更新并预览</Button>}<Button disabled={busy || !allowed('preview')} title={state.actions?.preview?.message} onClick={() => run('preview')}>预览已有数据</Button>{state.broker.capabilities.includes('prepare') && <Button disabled={busy || !allowed('prepare')} title={state.actions?.prepare?.message} onClick={() => run('prepare')}>仅准备订单</Button>}</div></details>
              <PreflightProgress job={state.preflight_job} onNavigate={navigate}/>
              {state.compatibility_issue && <p className="inline-warning">{state.compatibility_issue}</p>}<TargetTable preview={preview}/>
              {preview?.targets?.length > 0 && <div className="allocation"><span>目标股票仓位 <strong>{percent(preview.targets.reduce((a, b) => a + b.weight, 0))}</strong></span><span>其余保留为现金</span></div>}
              {preview?.orders?.length > 0 && <div className="table-wrap"><table><thead><tr><th>股票</th><th>方向</th><th className="right">股数</th><th className="right">限价（USD）</th><th className="right">金额（USD）</th></tr></thead><tbody>{preview.orders.map((o,i) => <tr key={i}><td>{o.ticker}</td><td>{orderSide(o.side)}</td><td className="right mono">{number(o.quantity)}</td><td className="right mono">{number(o.limit_price)}</td><td className="right mono">{number(o.notional)}</td></tr>)}</tbody></table><p className="small muted">报价预览仅短期有效；执行前再次检查账户和行情。超过单笔上限的调仓已拆单。</p></div>}<div className="execution-footer"><div><strong>{state.automation?.enabled ? '自动运行已开启' : preflight.noOrders ? '本轮无需提交订单' : preflight.showSubmit ? '模拟订单已准备，等待你的确认' : '准备下一轮'}</strong><p>{state.automation?.enabled ? state.automation.message : preflight.noOrders ? preview.message : preflight.showSubmit ? '核对股票、数量和当前模拟账户后，再提交给券商。' : state.preflight_job ? state.actions?.execute?.message : '点击「一键测试并准备」，前置检查会自动依次完成。'}</p></div>{preflight.showSubmit && <Button primary disabled={busy} onClick={submit}>提交模拟订单</Button>}</div>
              {preview?.issues?.length > 0 && <details className="details"><summary>查看数据说明（{preview.issues.length}）</summary><ul>{preview.issues.map((x, i) => <li key={i}>{typeof x === 'string' ? x : JSON.stringify(x)}</li>)}</ul></details>}
            </section></div></>;
}
