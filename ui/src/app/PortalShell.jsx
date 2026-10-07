import React from 'react';
import { navigation, sections } from './navigation';
import { visiblePlan } from './overview';
import { automationView } from '../automation';
import { isServerDeployment } from '../server';
import { Button, Icon } from '../ui';
import { cls, time } from '../format';

export function PortalShell({ route, navigate, access, onLogout, session, runtimeStatus, toggleRuntime, children }) {
  const { state, runtime, online, stopped } = session;
  const label = navigation.find(item => item.id === route.page)?.label;
  const plan = automationView(state?.automation, { online, runtimeRunning: runtime?.status === 'running' });
  const tabs = (sections[route.page] || []).filter(([id]) => isServerDeployment(access) || id !== 'server');
  return <div className="app-shell">
    <button className="skip-link" onClick={() => document.getElementById('main-content')?.focus()}>跳至内容</button>
    <aside className="sidebar">
      <div className="brand"><Icon name="chart" size={22}/><strong>Trading Bot</strong></div>
      <nav aria-label="主导航">
        {navigation.map(item => <button key={item.id} className={cls('nav-item', route.page === item.id && 'active', item.id === 'research' && 'nav-secondary-start')}
          aria-current={route.page === item.id ? 'page' : undefined} onClick={() => navigate(item.id)}>
          <Icon name={item.icon}/><span>{item.label}</span>
        </button>)}
      </nav>
      <div className="sidebar-foot">
        <span className="connection-state"><i className={online && !stopped ? 'on' : ''}/>{stopped ? '工作台已退出' : online ? '面板已连接' : '等待连接'}</span>
        <small>{runtime?.checked_at ? `确认于 ${time(runtime.checked_at)}` : '正在检查后台'}</small>
        <small>关闭网页后，已开启的后台继续运行。</small>
      </div>
    </aside>
    <div className="workspace">
      <header className="topbar">
        <span className="deployment-label">{isServerDeployment(access) ? '云端工作台' : '本地工作台'}<span>仅模拟账户</span></span>
        <div className="topbar-controls">
          {state?.strategy_manager?.running_count > 0 && <button className="text-link run-indicator" onClick={() => navigate('instances')}>{state.strategy_manager.running_count} 份实例已开启</button>}
          {visiblePlan(state?.automation) && <button className={cls('text-link run-indicator', plan.status === 'paused' && 'warning')} onClick={() => navigate('automation')}>单策略计划 · {plan.label}</button>}
          {!(route.page === 'system' && route.view === 'server') && <div className="runtime-control">
            <span className={runtimeStatus.tone} title={runtimeStatus.hint}>{runtimeStatus.label}</span>
            <button type="button" role="switch" aria-label="交易后台开关" aria-checked={runtimeStatus.checked}
              disabled={runtimeStatus.disabled} title={runtimeStatus.hint}
              className={cls('runtime-switch', runtimeStatus.checked && 'checked')} onClick={toggleRuntime}><span/></button>
          </div>}
          {access.enabled && <Button className="logout" onClick={onLogout}>退出登录</Button>}
        </div>
      </header>
      <main id="main-content" tabIndex="-1">
        <div className="page-heading"><h1>{label}</h1></div>
        {tabs.length > 0 && <nav className="section-tabs" aria-label={`${label}分类`}>
          {tabs.map(([view, text]) => <button key={view} aria-current={route.view === view ? 'page' : undefined}
            className={route.view === view ? 'active' : ''} onClick={() => navigate({ page: route.page, view })}>{text}</button>)}
        </nav>}
        {children}
      </main>
    </div>
  </div>;
}
