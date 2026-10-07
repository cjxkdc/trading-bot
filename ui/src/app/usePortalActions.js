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
      const result = await fn();
      if (result?.job_id) lastJob.current = result.job_id;
      setNotice(success || result?.message || '');
      if (refreshAfter) await refresh();
      return result;
    } catch (failure) { setError(failure.message); }
    finally { locked.current = false; setPending(false); }
  }
  return { act, pending, error, notice, setError, setNotice, run: action => act(() => api('jobs', { action })) };
}
