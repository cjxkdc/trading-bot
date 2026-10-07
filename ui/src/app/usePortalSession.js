import { useCallback, useEffect, useRef, useState } from 'react';
import { api, connect } from '../api';

export function usePortalSession() {
  const [state, setState] = useState(null);
  const [runtime, setRuntime] = useState(null);
  const [online, setOnline] = useState(false);
  const [stopped, setStopped] = useState(false);
  const [connectionError, setConnectionError] = useState('');
  const mounted = useRef(true), version = useRef(0), stoppedRef = useRef(false);

  const refresh = useCallback(async () => {
    const request = ++version.current;
    try {
      const current = await api('runtime', undefined, { timeoutMs: 5000 });
      if (!mounted.current || stoppedRef.current || request !== version.current) return;
      setRuntime(current); setOnline(true); setConnectionError('');
      if (current.status !== 'running') { setState(null); return; }
      const data = await api('state');
      if (mounted.current && !stoppedRef.current && request === version.current) setState(data);
    } catch (error) {
      if (mounted.current && !stoppedRef.current && request === version.current) {
        setOnline(false); setState(null); setConnectionError(error.message);
      }
      throw error;
    }
  }, []);

  useEffect(() => {
    mounted.current = true;
    let timer;
    async function poll(first = false) {
      try { if (first) await connect(); await refresh(); }
      catch (error) { if (mounted.current && !stoppedRef.current) { setOnline(false); setConnectionError(error.message); } }
      if (mounted.current && !stoppedRef.current) timer = setTimeout(poll, 2500);
    }
    poll(true);
    return () => { mounted.current = false; ++version.current; clearTimeout(timer); };
  }, [refresh]);

  function markStopped() {
    stoppedRef.current = true; ++version.current;
    setStopped(true); setOnline(false); setState(null); setConnectionError('');
  }
  return { state, runtime, online, stopped, connectionError, refresh, markStopped };
}
