// After an upgrade the open tab still references old chunk names. Reload once to
// pick up the new build. Without durable session storage, show the manual retry
// instead: an in-memory flag cannot prevent a reload loop across page loads.
function browserStorage() {
  try { return globalThis.sessionStorage; } catch { return undefined; }
}

export function reloadOnceForStaleBuild(error, storage = browserStorage(), location = globalThis.location) {
  if (!storage || !location?.reload) throw error;
  let last;
  try { last = Number(storage.getItem('trading-portal:chunk-reload')) || 0; } catch { throw error; }
  if (Date.now() - last < 60000) throw error;
  try { storage.setItem('trading-portal:chunk-reload', String(Date.now())); } catch { throw error; }
  location.reload();
  return new Promise(() => {});
}
