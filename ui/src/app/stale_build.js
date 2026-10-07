// After an upgrade the open tab still references old chunk names. Reload once to
// pick up the new build instead of leaving a blank page; never loop.
export function reloadOnceForStaleBuild(error, storage = globalThis.sessionStorage, location = globalThis.location) {
  let last = 0;
  try { last = Number(storage?.getItem('trading-portal:chunk-reload')) || 0; } catch { /* storage blocked */ }
  if (Date.now() - last < 60000) throw error;
  try { storage?.setItem('trading-portal:chunk-reload', String(Date.now())); } catch { /* storage blocked */ }
  location?.reload();
  return new Promise(() => {});
}
