"""Thin double-click entry point; starts/reuses the local service and opens a browser."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from urllib.request import ProxyHandler, build_opener
import webbrowser

from app.launcher import project_root, external_environment, external_dll_search
from app.execution_policy import CURRENT_EXECUTION_POLICY, load_execution_policy


def running_url(root):
    try:
        state = json.loads((root / 'runs/portal/server.json').read_text(encoding='utf-8'))
        port = state['port']
        if type(port) is not int or not 1 <= port <= 65535:
            return None
        url = f'http://127.0.0.1:{port}'
        with build_opener(ProxyHandler({})).open(url + '/health', timeout=1) as response:
            result = json.load(response)
        if result.get('application') == 'trading-bot-portal' and result.get('instance') == state['instance']:
            return url
    except (OSError, ValueError, KeyError, TypeError):
        return None


def start(root):
    url = running_url(root)
    if url:
        return url
    python = root / ('.venv/Scripts/python.exe' if os.name == 'nt' else '.venv/bin/python')
    required = [python, root / 'app/portal_server.py', root / 'portal/controller.py',
                root / 'portal/runtime.py', root / 'ui/dist/index.html']
    if any(not path.is_file() for path in required):
        raise ValueError('缺少项目环境或网页文件。请保留完整的 Trading bot 文件夹，并按 portal/README.md 安装。')
    with external_dll_search():
        process = subprocess.Popen([str(python), '-u', '-m', 'app.portal_server'], cwd=root,
                                   env=external_environment(), stdin=subprocess.DEVNULL,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
                                   start_new_session=os.name != 'nt')
    for _ in range(80):
        url = running_url(root)
        if url:
            return url
        if process.poll() is not None:
            # A simultaneous second launcher may lose the lock while the first starts.
            time.sleep(.25)
            url = running_url(root)
            if url:
                return url
            raise ValueError('无法启动面板。请关闭旧版启动器，并确认已安装 requirements.txt。')
        time.sleep(.25)
    raise ValueError('本地服务尚未就绪。请稍后再次双击启动器。')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--self-test', type=Path, help='Readiness report; never opens a browser or accesses a broker')
    args = parser.parse_args()
    try:
        url = start(project_root())
        if args.self_test:
            args.self_test.write_text(json.dumps({'ready': True, 'url': url,
                                                 'execution_policy': CURRENT_EXECUTION_POLICY.identifier,
                                                 'execution_scope': load_execution_policy(project_root()).public(),
                                                 'broker_requested': False}), encoding='utf-8')
        else:
            webbrowser.open(url)
    except (OSError, ValueError) as exc:
        if args.self_test:
            args.self_test.write_text(json.dumps({'ready': False, 'message': str(exc)}, ensure_ascii=False), encoding='utf-8')
        else:
            if os.name == 'nt':
                import ctypes
                ctypes.windll.user32.MessageBoxW(None, str(exc), 'Trading Bot', 0x10)
            else:
                print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
