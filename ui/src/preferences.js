// Per-browser display preferences. Storage can be unavailable (private mode,
// blocked site data); the page must still render with the defaults.
export const defaults = { theme: 'dark', updown: 'us' };
const allowed = { theme: ['dark', 'light'], updown: ['us', 'cn'] };
const key = name => `trading-portal:${name}`;

export function readPreference(name, storage = globalThis.localStorage) {
  try {
    const value = storage?.getItem(key(name));
    return allowed[name].includes(value) ? value : defaults[name];
  } catch {
    return defaults[name];
  }
}

export function savePreference(name, value, storage = globalThis.localStorage) {
  if (!allowed[name]?.includes(value)) throw new Error('Unknown display preference');
  try { storage?.setItem(key(name), value); } catch { /* Display still changes for this page. */ }
  return value;
}

export function applyPreferences(root = globalThis.document?.documentElement, storage = globalThis.localStorage) {
  if (!root) return;
  const theme = readPreference('theme', storage);
  root.dataset.theme = theme;
  root.dataset.updown = readPreference('updown', storage);
  globalThis.document?.querySelector('meta[name="theme-color"]')?.setAttribute('content', theme === 'dark' ? '#0e1219' : '#ffffff');
}
