let token;

function authRequired(message = '请先登录服务器工作台。') {
  window.dispatchEvent(new Event('portal-auth-required'));
  return new Error(message);
}

export async function authStatus() {
  const response = await request('/api/auth/status', { cache: 'no-store', credentials: 'same-origin' }, 5000);
  if (response.status === 404) return { enabled: false, authenticated: true, deployment: 'desktop' };
  if (!response.ok) throw new Error('工作台连接失败，请检查服务器状态。');
  return response.json();
}

export async function login(username, password, loginToken) {
  const response = await request('/api/auth/login', {
    method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json', 'X-Portal-Token': loginToken },
    body: JSON.stringify({ username, password }), cache: 'no-store',
  });
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(error.message || '登录失败，请重试。');
  }
  return response.json();
}

async function request(url, options = {}, timeoutMs = 15000) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try { return await fetch(url, { ...options, signal: controller.signal }); }
  catch (error) {
    if (options.method === 'POST') throw new Error('未收到操作确认，请检查后台状态和运行记录；不会自动重复提交。');
    throw new Error('未能确认后台状态，请检查工作台是否仍在运行。');
  } finally { clearTimeout(timer); }
}

export async function connect() {
  const response = await request('/api/session', { cache: 'no-store' }, 5000);
  if (response.status === 401) throw authRequired();
  if (!response.ok) throw new Error('工作台连接失败。');
  token = (await response.json()).token;
}

export async function api(path, value, { raw = false, download = false, timeoutMs = 15000 } = {}) {
  const headers = { 'X-Portal-Token': token };
  if (value !== undefined) headers['Content-Type'] = raw ? 'application/octet-stream' : 'application/json';
  const options = {
    method: value === undefined ? 'GET' : 'POST', headers, cache: 'no-store',
    body: value === undefined ? undefined : raw ? value : JSON.stringify(value),
  };
  let response = await request('/api/' + path, options, timeoutMs);
  if (response.status === 401) throw authRequired();
  // A restarted Portal changes its token. Retry only a read, never a mutation.
  if (response.status === 403 && value === undefined) {
    await connect();
    options.headers['X-Portal-Token'] = token;
    response = await request('/api/' + path, options, timeoutMs);
  }
  if (response.status === 401) throw authRequired();
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(error.message || '操作失败，请刷新后检查工作台。');
  }
  return download ? response.blob() : response.json();
}

export async function downloadCode(id) {
  const blob = await api(`experiments/${id}/code`, undefined, { download: true });
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url; link.download = 'main.py'; link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
