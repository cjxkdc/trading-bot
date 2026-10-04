import React, { useEffect, useState } from 'react';
import { authStatus, login, api } from './api';

export function PortalAccess({ children }) {
  const [access, setAccess] = useState(null), [error, setError] = useState('');
  const [username, setUsername] = useState('owner'), [password, setPassword] = useState('');
  const [busy, setBusy] = useState(false);
  async function check() {
    try { setAccess(await authStatus()); setError(''); }
    catch (e) { setError(e.message); }
  }
  useEffect(() => {
    let alive = true;
    authStatus().then(value => { if (alive) setAccess(value); }).catch(e => { if (alive) setError(e.message); });
    const expired = () => { setAccess(current => current ? { ...current, authenticated: false } : current); setPassword(''); };
    window.addEventListener('portal-auth-required', expired);
    return () => { alive = false; window.removeEventListener('portal-auth-required', expired); };
  }, []);
  async function submit(event) {
    event.preventDefault(); if (busy) return;
    setBusy(true); setError('');
    try { await login(username, password, access.login_token); setPassword(''); await check(); }
    catch (e) { setError(e.message); setPassword(''); }
    finally { setBusy(false); }
  }
  async function logout() {
    try { await api('auth/logout', {}); setAccess(current => ({ ...current, authenticated: false })); }
    catch (e) { setError(e.message); }
  }
  if (access && (!access.enabled || access.authenticated)) return children(access, logout);
  return <div className="login-screen"><section className="card login-card">
    <p className="eyebrow">TRADING BOT · SERVER</p><h1>服务器工作台</h1>
    <p className="subtitle">登录后查看服务器上的 bot，管理账户连接与运行状态。</p>
    {error && <div className="alert error" role="alert">{error}</div>}
    {!access ? <button className="button" onClick={check}>重新连接</button> : <form onSubmit={submit}>
      <label>工作台用户名<input required autoComplete="username" value={username} onChange={e => setUsername(e.target.value)} disabled={busy}/></label>
      <label>工作台密码<input required type="password" autoComplete="current-password" value={password} onChange={e => setPassword(e.target.value)} disabled={busy}/></label>
      <button className="button primary full" type="submit" disabled={busy}>{busy ? '登录中…' : '登录工作台'}</button>
      <small className="muted">使用服务器工作台密码。券商登录通过券商自己的授权入口完成。</small>
    </form>}
  </section></div>;
}
