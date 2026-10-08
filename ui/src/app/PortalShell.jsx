import React, { useEffect, useState } from 'react';
import { navigation, sections } from './navigation';
import { attentionItems, visiblePlan } from './overview';
import { automationView } from '../automation';
import { isServerDeployment } from '../server';
import { marketSession, marketClock } from '../market';
import { executionContext, validTimezone } from '../execution_context';
import { applyPreferences, readPreference, savePreference } from '../preferences';
import { Button, Icon } from '../ui';
import { cls, time } from '../format';

function useNow(interval = 1000) {
  const [now, setNow] = useState(() => new Date());
  useEffect(() => { const id = setInterval(() => setNow(new Date()), interval); return () => clearInterval(id); }, [interval]);
  return now;
}

function PreferenceToggle({ name, label, options }) {
  const [value, setValue] = useState(() => readPreference(name));
  function choose(next) {
    setValue(savePreference(name, next));
    const root = document.documentElement;
    applyPreferences(root, undefined, { ...root.dataset, [name]: next });
  }
  return <span className="pref-toggle" role="group" aria-label={label}>
    {options.map(([id, text, className]) => <button key={id} type="button" aria-pressed={value === id} className={className} onClick={() => choose(id)}>{text}</button>)}
  </span>;
}

export function PortalShell({ route, navigate, access, onLogout, session, runtimeStatus, toggleRuntime, children }) {
  const { state, runtime, online, stopped } = session;
  const current = navigation.find(item => item.id === route.page);
  const plan = automationView(state?.automation, { online, runtimeRunning: runtime?.status === 'running' });
  const tabs = (sections[route.page] || []).filter(([id]) => isServerDeployment(access) || id !== 'server');
  const now = useNow();
  const instance = route.page === 'runs' ? state?.strategy_manager?.instances?.find(item => item.id === route.instance) : null;
  const scopedBroker = instance ? state?.brokers?.find(item => item.id === instance.broker_id) : state?.broker;
  const context = executionContext(scopedBroker, instance?.execution_context);
  const market = marketSession(scopedBroker?.id === state?.broker?.id ? state?.market_session : null, now);
  const timezone = validTimezone(context.timezone) ? context.timezone : null;
  const attention = state ? attentionItems(state).length : 0;
  useEffect(() => {
    document.title = `${attention ? `(${attention}) ` : ''}${current?.label || '工作台'} · Trading Bot`;
  }, [attention, current?.label]);
  return <div className="app-shell">
    <button className="skip-link" onClick={() => document.getElementById('main-content')?.focus()}>跳至内容</button>
    <aside className="sidebar">
      <div className="brand"><Icon name="chart" size={18}/><strong>Trading Bot</strong></div>
      <nav aria-label="主导航">
        {navigation.map(item => <button key={item.id} className={cls('nav-item', route.page === item.id && 'active', item.id === 'connections' && 'nav-secondary-start')}
          aria-current={route.page === item.id ? 'page' : undefined} onClick={() => navigate(item.id)}>
          <Icon name={item.icon}/><span>{item.label}</span>
          {item.id === 'overview' && attention > 0 && <span className="nav-badge" aria-label={`${attention} 项需要处理`}>{attention}</span>}
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
        <div className="status-strip">
          <span className="clock" title={timezone || '当前设备本地时间'}>{marketClock(now, timezone)}<small>{timezone || '本地'}</small></span>
          <span className={cls('session', market.tone)} title="当前账户的市场与交易日历；缺少最新日历结果时不推测开盘状态。">{context.market || '市场未提供'} · {market.label}</span>
          <span className="divider" aria-hidden="true"/>
          <span className="deployment-label">{isServerDeployment(access) ? '云端工作台' : '本地工作台'}</span>
        </div>
        <div className="topbar-controls">
          {state?.strategy_manager?.running_count > 0 && <button className="text-link run-indicator" onClick={() => navigate('instances')}>{state.strategy_manager.running_count} 份实例已开启</button>}
          {visiblePlan(state?.automation) && <button className={cls('text-link run-indicator', plan.status === 'paused' && 'warning')} onClick={() => navigate('automation')}>单策略计划 · {plan.label}</button>}
          <PreferenceToggle name="updown" label="涨跌颜色" options={[['us', '绿涨', 'gain-us'], ['cn', '红涨', 'gain-cn']]}/>
          <PreferenceToggle name="theme" label="界面主题" options={[['dark', '深色'], ['light', '浅色']]}/>
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
        <div className="page-heading"><h1>{current?.label}</h1>{current?.caption && <span>{current.caption}</span>}</div>
        {tabs.length > 0 && <nav className="section-tabs" aria-label={`${current?.label}分类`}>
          {tabs.map(([view, text]) => <button key={view} aria-current={route.view === view ? 'page' : undefined}
            className={route.view === view ? 'active' : ''} onClick={() => navigate({ page: route.page, view })}>{text}</button>)}
        </nav>}
        {children}
      </main>
    </div>
  </div>;
}
