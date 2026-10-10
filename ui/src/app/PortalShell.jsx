import React, { useEffect, useState } from 'react';
import { navigation, intros, visibleTabs } from './navigation';
import { attentionItems } from './overview';
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

// Every page starts the same way: what this place is for, and its one main action.
export function PageHeader({ title, intro, action, back, onBack }) {
  return <div className="page-header-block">
    {back && <button type="button" className="page-back" onClick={onBack}><Icon name="back" size={14}/>{back}</button>}
    <div className="page-header"><div><h1>{title}</h1>{intro && <p>{intro}</p>}</div>{action && <div className="page-actions">{action}</div>}</div>
  </div>;
}

export function PortalShell({ route, navigate, access, onLogout, session, runtimeStatus, toggleRuntime, header, children }) {
  const { state, runtime, online, stopped } = session;
  const server = isServerDeployment(access);
  const current = navigation.find(item => item.id === route.page);
  const tabs = visibleTabs(route.page, server);
  const quiet = route.page === 'runs' && ['manual', 'plan'].includes(route.view);
  const now = useNow();
  const instance = route.page === 'runs' ? state?.strategy_manager?.instances?.find(item => item.id === route.instance) : null;
  const scopedBroker = instance ? state?.brokers?.find(item => item.id === instance.broker_id) : state?.broker;
  const context = executionContext(scopedBroker, instance?.execution_context);
  const market = marketSession(scopedBroker?.id === state?.broker?.id ? state?.market_session : null, now);
  const timezone = validTimezone(context.timezone) ? context.timezone : null;
  const attention = state ? attentionItems(state).length : 0;
  const title = header?.title || current?.label || '工作台';
  useEffect(() => {
    document.title = `${attention ? `(${attention}) ` : ''}${title} · Trading Bot`;
  }, [attention, title]);
  let lastGroup = null;
  return <div className="app-shell">
    <button className="skip-link" onClick={() => document.getElementById('main-content')?.focus()}>跳至内容</button>
    <aside className="sidebar">
      <div className="brand"><span className="brand-mark" aria-hidden="true"><Icon name="chart" size={16}/></span><span>Trading Bot</span></div>
      <nav aria-label="主导航">
        {navigation.map(item => {
          const group = item.group !== lastGroup ? <span className="nav-group" key={`group-${item.group}`}>{item.group}</span> : null;
          lastGroup = item.group;
          return <React.Fragment key={item.id}>{group}
            <button className={cls('nav-item', route.page === item.id && 'active')}
              aria-current={route.page === item.id ? 'page' : undefined} onClick={() => navigate(item.id)}>
              <Icon name={item.icon}/><span>{item.label}</span>
              {item.id === 'overview' && attention > 0 && <span className="nav-badge" aria-label={`${attention} 项需要处理`}>{attention}</span>}
            </button>
          </React.Fragment>;
        })}
      </nav>
      <div className="sidebar-foot">
        <span className="connection-state"><i className={online && !stopped ? 'on' : ''}/>{stopped ? '工作台已退出' : online ? '面板已连接' : '等待连接'}</span>
        <small>{server ? '云端工作台' : '本地工作台'}{runtime?.checked_at ? ` · 确认于 ${time(runtime.checked_at)}` : ''}</small>
        <small>关闭网页后，已开启的后台继续运行。</small>
      </div>
    </aside>
    <div className="workspace">
      <header className="topbar">
        <div className="status-strip">
          <span className="clock" title={timezone || '当前设备本地时间'}>{marketClock(now, timezone)}<small>{timezone || '本地'}</small></span>
          <span className={cls('session', market.tone)} title="当前账户的市场与交易日历；缺少最新日历结果时不推测开盘状态。">{context.market || '市场未提供'} · {market.label}</span>
        </div>
        <div className="topbar-controls">
          <PreferenceToggle name="updown" label="涨跌颜色" options={[['us', '绿涨', 'gain-us'], ['cn', '红涨', 'gain-cn']]}/>
          <PreferenceToggle name="theme" label="界面主题" options={[['light', '浅色'], ['dark', '深色']]}/>
          {!(route.page === 'system' && route.view === 'server') && <div className="runtime-control">
            <span className={runtimeStatus.tone} title={runtimeStatus.hint}>交易后台 · {runtimeStatus.label}</span>
            <button type="button" role="switch" aria-label="交易后台开关" aria-checked={runtimeStatus.checked}
              disabled={runtimeStatus.disabled} title={runtimeStatus.hint}
              className={cls('runtime-switch', runtimeStatus.checked && 'checked')} onClick={toggleRuntime}><span/></button>
          </div>}
          {access.enabled && <Button className="logout" onClick={onLogout}>退出登录</Button>}
        </div>
      </header>
      <main id="main-content" tabIndex="-1">
        <PageHeader title={title} intro={header?.intro ?? intros[route.page]} action={header?.action} back={header?.back} onBack={header?.onBack}/>
        {tabs.length > 1 && !quiet && !header?.hideTabs && <nav className="section-tabs" aria-label={`${current?.label}分类`}>
          {tabs.map(([view, text]) => <button key={view} aria-current={route.view === view ? 'page' : undefined}
            className={route.view === view ? 'active' : ''} onClick={() => navigate({ page: route.page, view, broker: route.broker })}>{text}</button>)}
        </nav>}
        {children}
      </main>
    </div>
  </div>;
}
