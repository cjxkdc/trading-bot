"""Render the user service for an explicitly supplied, verified project path."""
from pathlib import Path
import sys


def render(project):
    project = Path(project).resolve()
    value = str(project)
    if any(c.isspace() or c in '%\\"' or ord(c) < 32 for c in value):
        raise ValueError('Unsupported service path')
    if not (project / 'app/portal_server.py').is_file():
        raise ValueError('Project entrypoint missing')
    return f'''[Unit]
Description=Trading Bot simulation Portal
After=network-online.target

[Service]
Type=simple
WorkingDirectory={value}
ExecStart={value}/.venv/bin/python -m app.portal_server --server --port 5329
Environment=PYTHONUTF8=1
Environment=PYTHONDONTWRITEBYTECODE=1
UMask=0077
NoNewPrivileges=true
Restart=on-failure
RestartSec=5
KillMode=process
TimeoutStopSec=infinity

[Install]
WantedBy=default.target
'''


if __name__ == '__main__':
    if len(sys.argv) != 3:
        raise SystemExit('Usage: render_service.py PROJECT OUTPUT')
    Path(sys.argv[2]).write_text(render(sys.argv[1]), encoding='utf-8')
