import { useEffect, useRef, useState } from 'react';
import { api } from '../api';

export function usePortalActions({ state, stopped, refresh }) {
  const [pending, setPending] = useState(false);
  const [error, setError] = useState(''), [notice, setNotice] = useState('');
  const locked = useRef(false), lastJob = useRef(null);
  useEffect(() => {
    const job = state?.jobs?.find(item => item.id === lastJob.current);
    if (!job || job.status === 'running') return;
    lastJob.current = null;
    if (job.action === 'preflight') return;
    if (['blocked', 'paused', 'interrupted'].includes(job.status)) setError(job.message);
    else setNotice(job.message);
  }, [state]);

  async function act(fn, success = '', { refreshAfter = true } = {}) {
    if (locked.current || stopped) return;
    locked.current = true; setPending(true); setError(''); setNotice('');
    try {
      let result;
      try { result = await fn(); }
      catch (failure) { setError(failure.message); return undefined; }
      if (result?.job_id) lastJob.current = result.job_id;
      setNotice(success || result?.message || '');
      if (refreshAfter) {
        // The server already accepted the operation. A failed status refresh is
        // reported separately so callers never mistake it for a failed operation
        // (and ask the user to submit it again).
        try { await refresh(); }
        catch (failure) { if (!failure?.stale) setError(`操作已提交，但刷新工作台状态失败：${failure.message}`); }
      }
      return result;
    } finally { locked.current = false; setPending(false); }
  }
  return { act, pending, error, notice, setError, setNotice, run: action => act(() => api('jobs', { action })) };
}
