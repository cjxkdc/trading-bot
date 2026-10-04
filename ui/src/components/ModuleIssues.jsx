import React from 'react';

export function ModuleIssues({ items }) {
  const unavailable = items.filter(item => item.enabled === false);
  if (!unavailable.length) return null;
  return <details className="details"><summary>未启用模块（{unavailable.length}）</summary>
    <ul>{unavailable.map(item => <li key={item.id}>{item.name} · {item.reason}</li>)}</ul>
  </details>;
}
