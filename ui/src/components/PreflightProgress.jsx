import React from 'react';
import { preflightProgress } from '../preflight';

const labels = { pending: '待检查', running: '检查中', passed: '通过', blocked: '待处理', skipped: '已跳过' };
const symbols = { pending: '·', running: '·', passed: '✓', blocked: '!', skipped: '—' };
const destinations = { account: ['broker', '查看账户设置'], data: ['data', '查看数据设置'],
  refresh: ['data', '查看数据状态'], preview: ['strategies', '查看策略设置'] };

export function PreflightProgress({ job, onNavigate }) {
  const progress = preflightProgress(job);
  if (!progress) return null;
  const destination = destinations[progress.blocked?.id];
  return <section className="preflight-progress" aria-label="一键测试结果">
    <div className="preflight-heading"><strong>本轮测试</strong><span>{progress.summary}</span></div>
    <ol className="preflight-steps">{progress.steps.map((step, index) => <li key={step.id || index} className={'preflight-step ' + step.status + (step.warnings.length ? ' has-warning' : '')} title={step.message || undefined}>
      <span className="preflight-step-icon" aria-hidden="true">{symbols[step.status]}</span><div><strong>{step.label}</strong><small>{labels[step.status]}{step.warnings.length > 0 ? ' · 有说明' : ''}</small></div>
    </li>)}</ol>
    {progress.warnings.length > 0 && <details className="preflight-warnings"><summary>查看检查说明（{progress.warnings.length} 项）</summary>{progress.warnings.map(step => <div key={step.id}><strong>{step.label}</strong><ul>{step.warnings.map((warning, i) => <li key={i}>{warning}</li>)}</ul></div>)}</details>}
    {progress.message && <div className={'preflight-result' + (progress.blocked ? ' blocked' : '')} role="status"><p>{progress.message}</p>{destination && onNavigate && <button className="text-link" onClick={() => onNavigate(destination[0])}>{destination[1]} →</button>}</div>}
  </section>;
}
