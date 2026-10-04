"""Build a thin Windows executable. Copy it beside bot.py, keeping the project venv."""
import argparse
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, default=ROOT / 'dist')
    args = parser.parse_args()
    if sys.platform != 'win32':
        parser.error('Build the Windows .exe on Windows.')
    work = ROOT / 'build/launcher'
    work.mkdir(parents=True, exist_ok=True)
    subprocess.run([sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean', '--onefile',
                    '--windowed', '--noupx', '--paths', str(ROOT), '--exclude-module', 'tkinter', '--icon', 'NONE', '--name', 'Trading Bot',
                    '--distpath', str(args.out.resolve()), '--workpath', str(work / 'temp'),
                    '--specpath', str(work), str(ROOT / 'app/portal_launcher.py')], cwd=ROOT, check=True)
    print(args.out.resolve() / 'Trading Bot.exe')


if __name__ == '__main__':
    main()
