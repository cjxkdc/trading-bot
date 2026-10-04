"""Local portal process. Kept outside the browser and outside the launcher exe."""
import argparse
import os
from pathlib import Path
import socket
from uuid import uuid4

from app.launcher import single_instance
from portal.controller import create_controller
from app.workbench import atomic_json


def bind_portal_socket(sock, port):
    # On POSIX a graceful restart can encounter prior TCP connections in
    # TIME_WAIT. Reuse that closed endpoint, never another active listener.
    # Windows SO_REUSEADDR can permit unsafe same-port sharing; keep it off.
    if os.name == 'posix':
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(('127.0.0.1', port))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=0)
    parser.add_argument('--server', action='store_true', help='Require configured server Portal login')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    access = None
    if args.server:
        from portal.access import RemoteSettings
        access = RemoteSettings.load(root)
    import uvicorn
    with single_instance(root):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            bind_portal_socket(sock, args.port)
            port = sock.getsockname()[1]
            instance = uuid4().hex
            server = None

            def shutdown():
                server.should_exit = True

            app = create_controller(root, port=port, instance=instance, shutdown=shutdown, access=access)
            server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port,
                                      access_log=False, log_level='critical'))
            state = root / 'runs/portal/server.json'
            atomic_json(state, {'port': port, 'pid': os.getpid(), 'instance': instance})
            try:
                app.state.runtime.autostart()
                server.run(sockets=[sock])
            finally:
                app.state.runtime.close()
                state.unlink(missing_ok=True)


if __name__ == '__main__':
    main()
