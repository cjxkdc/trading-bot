import React from 'react';
import { cls, percent } from './format';
export function Icon({ name, size = 18 }) {
  const paths = {
    grid: <><rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/></>,
    layers: <><path d="m12 3 9 5-9 5-9-5 9-5Zm-9 9 9 5 9-5M3 16l9 5 9-5"/></>,
    database: <><ellipse cx="12" cy="5" rx="8" ry="3"/><path d="M4 5v14c0 4 16 4 16 0V5M4 12c0 4 16 4 16 0"/></>,
    link: <><path d="m9 15 6-6M8 16l-1 1a4.2 4.2 0 0 1-6-6l4-4a4.2 4.2 0 0 1 6 0m2 10a4.2 4.2 0 0 0 6 0l4-4a4.2 4.2 0 0 0-6-6l-1 1" transform="translate(1 0) scale(.92)"/></>,
    briefcase: <><rect x="3" y="7" width="18" height="14" rx="2"/><path d="M8 7V4h8v3M3 12c6 4 12 4 18 0M10 13v3h4v-3"/></>,
    chart: <><path d="M4 3v17h17M7 14l4-4 4 2 5-7"/></>,
    list: <><path d="M9 5h12M9 12h12M9 19h12M3 5h1M3 12h1M3 19h1"/></>,
    arrow: <><path d="M5 12h14m-5-5 5 5-5 5"/></>,
    refresh: <><path d="M20 8a8 8 0 0 0-14-3L3 8m0-5v5h5M4 16a8 8 0 0 0 14 3l3-3m0 5v-5h-5"/></>,
    check: <path d="m5 12 4 4L19 6"/>,
    clock: <><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></>,
    upload: <><path d="M12 16V3m-5 5 5-5 5 5M4 16v5h16v-5"/></>,
    power: <><path d="M12 2v10M6 5a9 9 0 1 0 12 0"/></>,
    server: <><rect x="3" y="3" width="18" height="7" rx="2"/><rect x="3" y="14" width="18" height="7" rx="2"/><path d="M7 6.5h.01M7 17.5h.01M13 6.5h4M13 17.5h4"/></>,
    close: <path d="m6 6 12 12M6 18 18 6"/>,
  };
  return <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{paths[name] || paths.grid}</svg>;
}
export function Button({ children, primary, icon, className, ...props }) {
  return <button className={cls('button', primary && 'primary', className)} {...props}>{icon && <Icon name={icon}/>}<span>{children}</span></button>;
}
export function Empty({ title, children, icon = 'layers' }) {
  return <div className="empty"><span className="empty-icon"><Icon name={icon} size={24}/></span><strong>{title}</strong><p>{children}</p></div>;
}
export function Tag({ children, tone = 'muted' }) { return <span className={'tag ' + tone}>{children}</span>; }

export function TargetTable({ preview }) {
  const rows = preview?.targets || [];
  if (!rows.length) return <Empty title={preview ? '本次没有选中股票' : '尚未生成目标持仓'}>{preview ? '检查筛选条件与数据覆盖范围。' : '更新数据或使用已有快照，运行一次策略预览。'}</Empty>;
  return <div className="table-wrap"><table><thead><tr><th>股票</th><th>行业</th><th className="right">策略得分</th><th className="right">目标权重</th></tr></thead><tbody>{rows.map(row => <tr key={row.asset_id}><td><strong>{row.ticker || row.asset_id}</strong></td><td className="muted">{row.industry || '—'}</td><td className="right mono">{Number(row.score ?? 0).toFixed(3)}</td><td className="right mono"><span className="weight-cell"><i style={{ width: `${Math.min(100, row.weight * 200)}%` }}/><span>{percent(row.weight)}</span></span></td></tr>)}</tbody></table></div>;
}
