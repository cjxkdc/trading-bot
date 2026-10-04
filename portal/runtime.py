"""Own the trading backend process; the Portal remains available when it is off."""
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import multiprocessing as mp
import os
from pathlib import Path
import socket
import sys
import threading
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from uuid import uuid4

from app.control_contracts import ControlError
from portal.service import atomic_json, now


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


@contextmanager
def backend_lock(root):
    """A replacement Portal cannot start a second engine while an old one drains."""
    path = Path(root) / 'runs/portal/backend.lock'
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as handle:
        if handle.tell() == 0:
            handle.write(b'0'); handle.flush()
        handle.seek(0)
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == 'nt':
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


def run_backend(root, channel, stop_channel):
    """Spawn entry point. Default is idle; an explicitly armed plan restores its journal checks."""
    try:
        import uvicorn
        from portal.app import create_app
        with backend_lock(root), socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(('127.0.0.1', 0))
            port, instance = sock.getsockname()[1], uuid4().hex
            server = None
            app = create_app(root, port=port, instance=instance,
                             shutdown=lambda: setattr(server, 'should_exit', True))
            server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port,
                                                 access_log=False, log_level='critical', log_config=None))

            def watch_owner():
                parent = mp.parent_process()
                try:
                    while not server.should_exit:
                        try:
                            requested = stop_channel.poll(.5)
                            if requested:
                                stop_channel.recv_bytes()
                        except (EOFError, OSError):
                            requested = True
                        if not requested and (parent is None or parent.is_alive()):
                            continue
                        # Losing the Portal must not interrupt an order transmission.
                        with app.state.portal.lock:
                            app.state.portal.closing = True
                            worker = app.state.portal.thread
                        if worker and worker.is_alive():
                            worker.join()
                        server.should_exit = True
                        return
                finally:
                    stop_channel.close()

            threading.Thread(target=watch_owner, name='portal-owner-watch', daemon=True).start()
            channel.send({'port': port, 'instance': instance, 'token': app.state.portal_token})
            channel.close()
            try:
                server.run(sockets=[sock])
            finally:
                worker = app.state.portal.thread
                if worker and worker.is_alive():
                    worker.join()
    except Exception:
        try:
            channel.send({'error': True})
        except (OSError, EOFError):
            pass
    finally:
        channel.close()
        stop_channel.close()


@dataclass
class Reply:
    status: int
    body: bytes
    headers: dict


class BackendRuntime:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.preferences = self.root / 'runs/portal/runtime-preferences.json'
        self.lock = threading.RLock()
        self.process = self.stop_channel = self.info = None
        self.phase, self.started_at = 'stopped', None
        self.reader = None
        self.closing = False
        self.context = mp.get_context('spawn')
        if os.name == 'nt':
            pythonw = Path(sys.executable).with_name('pythonw.exe')
            if pythonw.is_file():
                mp.set_executable(str(pythonw))

    def autostart(self):
        enabled = True
        if self.preferences.is_file():
            try:
                enabled = json.loads(self.preferences.read_text(encoding='utf-8'))['enabled'] is True
            except (OSError, ValueError, KeyError, TypeError):
                enabled = False
        if enabled:
            self.start(persist=False)

    def _save(self, enabled):
        atomic_json(self.preferences, {'enabled': enabled})

    def _alive(self):
        return self.process is not None and self.process.is_alive()

    def _reconcile(self):
        if self.process is not None and not self._alive():
            self.process.join(timeout=0)
            self.info = None
            if self.stop_channel:
                self.stop_channel.close()
                self.stop_channel = None
            if self.phase not in {'stopped', 'failed'}:
                self.phase = 'stopped' if self.phase == 'stopping' else 'failed'
            self.started_at = None

    def _request(self, method, path, content=None, content_type=None, timeout=10):
        # Only private, fixed loopback endpoints. Never forward caller credentials.
        with self.lock:
            info = dict(self.info or {})
        if not info:
            raise ControlError('交易后台尚未就绪。')
        headers = {'X-Portal-Token': info['token']}
        if content_type:
            headers['Content-Type'] = content_type
        request = Request(f"http://127.0.0.1:{info['port']}" + path, data=content,
                          headers=headers, method=method)
        try:
            response = build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=timeout)
        except HTTPError as exc:
            response = exc
        with response:
            return Reply(response.status, response.read(), {key: value for key, value in response.headers.items()
                         if key.lower() in {'content-type', 'content-disposition'}
                         or key.lower() == 'location' and value == '/'})

    def status(self):
        with self.lock:
            self._reconcile()
            phase, info = self.phase, self.info
        if phase in {'starting', 'running', 'unresponsive'} and info:
            try:
                reply = self._request('GET', '/health', timeout=1)
                health = json.loads(reply.body)
                responsive = reply.status == 200 and health.get('instance') == info['instance']
                responsive = responsive and health.get('application') == 'trading-bot-portal'
            except (OSError, ValueError, ControlError):
                responsive = False
            with self.lock:
                if self.info is info and self.phase in {'starting', 'running', 'unresponsive'}:
                    if responsive:
                        self.phase = 'running'
                        self.started_at = self.started_at or now()
                    elif self.phase != 'starting':
                        self.phase = 'unresponsive'
        with self.lock:
            self._reconcile()
            return {'status': self.phase, 'alive': self._alive(), 'started_at': self.started_at,
                    'checked_at': now(), 'pid': self.process.pid if self._alive() else None,
                    'uptime_seconds': max(0, int((datetime.now(timezone.utc) - datetime.fromisoformat(self.started_at)).total_seconds())) if self.started_at else None,
                    'schedule': 'manual'}

    def start(self, persist=True):
        with self.lock:
            self._reconcile()
            if self.closing:
                raise ControlError('工作台正在退出。')
            if self._alive():
                if self.phase == 'stopping':
                    raise ControlError('请等待后台完全停止后再开启。')
                return self.status()
            receiving, sending = self.context.Pipe(duplex=False)
            stopping, self.stop_channel = self.context.Pipe(duplex=False)
            self.process = self.context.Process(target=run_backend,
                args=(str(self.root), sending, stopping), name='trading-backend', daemon=False)
            self.info, self.started_at, self.phase = None, None, 'starting'
            try:
                self.process.start()
            except Exception:
                self.process, self.phase = None, 'failed'
                receiving.close(); sending.close()
                stopping.close(); self.stop_channel.close()
                self.stop_channel = None
                raise ControlError('交易后台未能启动，请检查项目环境。') from None
            sending.close()
            stopping.close()
            if persist:
                self._save(True)
            process, stop_channel = self.process, self.stop_channel

            def await_ready():
                try:
                    if not receiving.poll(15):
                        raise ValueError('startup timeout')
                    info = receiving.recv()
                    if not isinstance(info, dict) or set(info) != {'port', 'instance', 'token'}:
                        raise ValueError('startup failed')
                    with self.lock:
                        if self.process is process:
                            self.info = info
                except (OSError, EOFError, ValueError):
                    with self.lock:
                        if self.process is process:
                            self.phase = 'failed'
                            self._signal_stop(stop_channel)
                finally:
                    receiving.close()

            self.reader = threading.Thread(target=await_ready, name='backend-startup', daemon=True)
            self.reader.start()
            return {'status': 'starting', 'message': '正在启动交易后台；已开启的自动计划会恢复检查，请核对自动运行状态。'}

    def forward(self, method, path, content=None, content_type=None):
        with self.lock:
            self._reconcile()
            if self.closing or self.phase not in {'running', 'unresponsive'} or not self._alive():
                raise ControlError('交易后台尚未运行，请先打开后台开关。')
        try:
            return self._request(method, path, content, content_type, timeout=30)
        except (OSError, ValueError):
            raise ControlError('后台没有返回有效结果。请核对运行记录；操作不会自动重试。') from None

    def stop(self, persist=True):
        with self.lock:
            self._reconcile()
            if not self._alive():
                self.phase = 'stopped'
                if persist:
                    self._save(False)
                return {'status': 'stopped', 'message': '交易后台已停止，面板仍可使用。'}
            if self.phase == 'stopping':
                return {'status': 'stopping', 'message': '正在等待交易后台退出。'}
            if self.phase != 'running':
                raise ControlError('后台正在启动或状态待核对，暂不强制停止。')
            try:
                reply = self._request('POST', '/api/shutdown', b'{}', 'application/json', timeout=5)
            except (OSError, ValueError):
                raise ControlError('没有收到停止确认，请等待状态更新；未强制终止后台。') from None
            if reply.status != 200:
                raise ControlError('当前仍有任务执行，不能直接停止后台。请等待任务结束。')
            self.phase = 'stopping'
            if persist:
                self._save(False)
            return {'status': 'stopping', 'message': '正在停止交易后台，面板将保持打开。'}

    def close(self):
        with self.lock:
            self.closing = True
            process = self.process
            self._signal_stop(self.stop_channel)
        if process:
            process.join()  # An in-flight order worker must finish; never terminate/kill.
        if self.reader:
            self.reader.join(timeout=1)
        if self.stop_channel:
            self.stop_channel.close()

    @staticmethod
    def _signal_stop(channel):
        if channel:
            try:
                channel.send_bytes(b'stop')
            except (BrokenPipeError, OSError):
                pass
