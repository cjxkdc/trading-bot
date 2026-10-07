// Per-tab memory only: survives re-login, never writes strategy source to browser
// storage. The original revision remains attached to prevent stale overwrites.
const drafts = new Map();
function beforeUnload(event) {
  if (!drafts.size) return;
  event.preventDefault(); event.returnValue = '';
}
export function rememberDraft(type, file, content) {
  if (content === file.content) { forgetDraft(type); return; }
  if (!drafts.size && typeof window !== 'undefined') window.addEventListener('beforeunload', beforeUnload);
  drafts.set(type, { file: { ...file }, content });
}
export function recallDraft(type) { return drafts.get(type); }
export function forgetDraft(type) {
  drafts.delete(type);
  if (!drafts.size && typeof window !== 'undefined') window.removeEventListener('beforeunload', beforeUnload);
}
