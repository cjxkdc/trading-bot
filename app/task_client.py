"""Hidden subprocess transport for module operations, with credentials on stdin only."""
import json
import os
from pathlib import Path
import subprocess

from app.launcher import external_dll_search, external_environment
from app.portal_worker import ACTIONS


def run_worker(root, action, snapshot=None, token=None, progress=None, *, source_id, broker_id,
               expected_context=None, credentials=None, automation=None):
    root = Path(root).resolve()
    request = {'action': action, 'source_id': source_id, 'broker_id': broker_id,
               'expected_context': expected_context, 'snapshot_path': str(snapshot) if snapshot else None,
               'preview_token': token, 'credentials': credentials, 'automation': automation}
    try:
        python = root / ('.venv/Scripts/python.exe' if os.name == 'nt' else '.venv/bin/python')
        if action not in ACTIONS or not python.is_file() or not (root / 'app/portal_worker.py').is_file():
            raise ValueError('请保留完整项目及 Python 环境。')
        streaming = action in {'refresh', 'data-status', 'prepare'}
        with external_dll_search():
            process = subprocess.Popen([str(python), '-u', '-m', 'app.portal_worker'], cwd=root,
                env=external_environment(), shell=False, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT if streaming else subprocess.PIPE, text=True,
                encoding='utf-8', errors='replace', creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        payload = json.dumps(request, ensure_ascii=True)
        try:
            if streaming:
                process.stdin.write(payload)
                process.stdin.close()
                output = ''
                for line in process.stdout:
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(event, dict):
                        continue
                    if event.get('portal_progress') == 1 and progress:
                        progress(event.get('message', ''))
                    elif event.get('portal_protocol') == 1:
                        output = line
                process.wait()
            else:
                output, _ = process.communicate(payload, timeout=None if action in {'execute', 'recover'} else 60)
        except subprocess.TimeoutExpired:
            process.kill()  # Never used on an execution worker.
            process.communicate()
            raise ValueError('只读检查超时，请检查模块连接后手动重试。') from None
        result = json.loads(output.strip().splitlines()[-1]) if output.strip() else {}
        if (result.get('portal_protocol') != 1 or result.get('action') != action
                or result.get('status') not in {'connected', 'ready', 'blocked', 'paused', 'previewed', 'complete'}
                or process.returncode not in (0, 2)):
            raise ValueError('没有取得可核对的结果，请检查记录；不会自动重试。')
        return result
    except (OSError, ValueError, AttributeError):
        return {'portal_protocol': 1, 'action': action, 'status': 'paused' if action == 'execute' else 'blocked',
                'message': '模块进程未返回有效结果，请检查项目环境；不会自动重试。', 'automatic_retry': False}
