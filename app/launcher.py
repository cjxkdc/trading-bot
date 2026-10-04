"""Small Windows desktop launcher. Bundle this file only, not the trading project."""
import argparse
from contextlib import contextmanager
from datetime import datetime
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
from uuid import uuid4

ACTIONS = ('offline', 'status', 'preview', 'execute', 'demo', 'refresh', 'data-status', 'configure-data')
LABELS = {'offline': '检查本地环境', 'status': '检查模拟盘连接', 'preview': '预览策略',
          'execute': '执行一轮模拟交易', 'demo': '运行离线演示', 'refresh': '更新科技股数据',
          'data-status': '检查数据权限', 'configure-data': '保存 Massive 配置'}
SOURCES = {'Massive': 'massive', 'Futu': 'futu'}


def project_root():
    if getattr(sys, 'frozen', False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


def backend_command(root, action, snapshot=None, token=None, *, data_source='futu'):
    if action not in ACTIONS:
        raise ValueError('启动器不支持此操作。')
    root = Path(root).resolve()
    python = root / ('.venv/Scripts/python.exe' if os.name == 'nt' else '.venv/bin/python')
    for path in (python, root / 'app/launcher_worker.py', root / 'configs/futu_paper_config.json'):
        if not path.is_file():
            raise ValueError('缺少项目文件或 Python 环境。请把启动器放在完整的 Trading bot 文件夹内。')
    command = [str(python), '-u', '-m', 'app.launcher_worker', action]
    if action in {'refresh', 'data-status', 'configure-data'}:
        if data_source not in SOURCES.values():
            raise ValueError('数据源选项无效。')
        command += ['--data-source', data_source]
    if action in {'preview', 'execute'}:
        if not snapshot or not Path(snapshot).is_file():
            raise ValueError('请先选择存在的行情快照 JSON。')
        command += ['--snapshot', str(Path(snapshot).resolve())]
    if action == 'execute':
        if not token:
            raise ValueError('请先预览策略。')
        command += ['--preview-token', token]
    return command


def external_environment():
    env = os.environ.copy()
    for name in ('PYTHONHOME', 'PYTHONPATH', 'TCL_LIBRARY', 'TK_LIBRARY'):
        env.pop(name, None)
    env.update(PYTHONUTF8='1', PYTHONIOENCODING='utf-8', PYTHONUNBUFFERED='1',
               PYTHONDONTWRITEBYTECODE='1')
    if getattr(sys, 'frozen', False):
        bundle = str(Path(sys._MEIPASS).resolve())
        env['PATH'] = os.pathsep.join(p for p in env.get('PATH', '').split(os.pathsep)
                                     if p and not str(Path(p).resolve()).startswith(bundle + os.sep)
                                     and str(Path(p).resolve()) != bundle)
        if os.name != 'nt':
            env['LD_LIBRARY_PATH'] = env.get('LD_LIBRARY_PATH_ORIG', '')
    return env


@contextmanager
def external_dll_search():
    # PyInstaller's bundled Python DLL must not override the project's venv DLL.
    frozen_windows = os.name == 'nt' and getattr(sys, 'frozen', False)
    if frozen_windows:
        import ctypes
        ctypes.windll.kernel32.SetDllDirectoryW(None)
    try:
        yield
    finally:
        if frozen_windows:
            ctypes.windll.kernel32.SetDllDirectoryW(str(sys._MEIPASS))


def run_worker(root, action, snapshot=None, token=None, progress=None, *, data_source='futu', credentials=None):
    try:
        command = backend_command(root, action, snapshot, token, data_source=data_source)
        streaming = action in {'refresh', 'data-status'}
        with external_dll_search():
            process = subprocess.Popen(command, cwd=root, env=external_environment(), shell=False,
                                       stdin=subprocess.PIPE if action == 'configure-data' else subprocess.DEVNULL,
                                       stdout=subprocess.PIPE,
                                       stderr=subprocess.STDOUT if streaming else subprocess.PIPE,
                                       text=True, encoding='utf-8', errors='replace',
                                       creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        try:
            # Never terminate a worker that could be transmitting an order.
            if streaming:
                output = ''
                for line in process.stdout:
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(event, dict):
                        continue
                    if event.get('launcher_progress') == 1 and isinstance(event.get('message'), str):
                        if progress:
                            progress(event['message'])
                    elif event.get('launcher_protocol') == 1:
                        output = line
                process.wait()
            else:
                if action == 'configure-data':
                    output, _ = process.communicate(input=json.dumps(credentials or {}), timeout=30)
                else:
                    output, _ = process.communicate(timeout=60 if action in {'offline', 'status', 'preview'} else None)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            raise ValueError('只读检查超时，请检查 OpenD 后手动重试。') from None
        lines = output.strip().splitlines()
        result = json.loads(lines[-1]) if lines else {}
        if (not isinstance(result, dict) or result.get('launcher_protocol') != 1
                or result.get('action') != action or not isinstance(result.get('message'), str)
                or result.get('status') not in {'connected', 'ready', 'blocked', 'paused', 'previewed', 'complete'}
                or process.returncode not in (0, 2)
                or (process.returncode == 2 and result['status'] not in {'blocked', 'paused'})):
            raise ValueError('进程没有返回可核对的结果。请检查账户与原有账本，程序不会自动重试。')
        return result
    except ValueError as exc:
        message = str(exc) if not isinstance(exc, json.JSONDecodeError) else '进程返回格式异常，请保留账本并核对状态。'
    except OSError:
        message = '无法启动项目 Python，请检查 .venv 和项目文件。'
    return {'launcher_protocol': 1, 'action': action,
            'status': 'paused' if action == 'execute' else 'blocked',
            'message': message, 'automatic_retry': False}


@contextmanager
def single_instance(root):
    directory = Path(root) / 'runs/launcher'
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / 'app.launcher.lock').open('a+b') as handle:
        if handle.tell() == 0:
            handle.write(b'0')
            handle.flush()
        handle.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise ValueError('这个项目的启动器已经打开，请使用现有窗口。') from None
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == 'nt':
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class Launcher:
    def __init__(self, window, root, *, autocheck=True):
        import tkinter as tk
        from tkinter import filedialog, messagebox, ttk
        from tkinter.scrolledtext import ScrolledText
        self.tk, self.ttk = tk, ttk
        self.filedialog, self.messagebox = filedialog, messagebox
        self.window, self.root = window, Path(root)
        self.events = queue.Queue()
        self.busy, self.connected, self.preview = False, False, None
        self.active_action = None
        self.can_cancel_download = False
        self.source = tk.StringVar(value='Massive')
        self.snapshot = tk.StringVar()
        self.connection = tk.StringVar(value='尚未检查连接')
        self.strategy = tk.StringVar(value='正在读取策略配置…')
        self.state = tk.StringVar(value='等待操作')
        self.hint = tk.StringVar(value='Massive：先配置 API Key，再检查数据权限、下载并预览。券商独立使用 Futu 模拟盘。')
        self.controls = []
        window.title('Trading Bot · Futu 模拟盘')
        window.geometry('1040x880')
        window.minsize(940, 810)
        window.configure(bg='#f3f6fb')
        window.protocol('WM_DELETE_WINDOW', self.close)
        style = ttk.Style(window)
        style.theme_use('clam')
        font = 'Microsoft YaHei UI' if os.name == 'nt' else 'sans-serif'
        window.option_add('*Font', (font, 10))
        style.configure('.', font=(font, 10))
        style.configure('TFrame', background='#f3f6fb')
        style.configure('TLabel', background='#f3f6fb', foreground='#18324d')
        style.configure('Card.TFrame', background='white')
        style.configure('Card.TLabel', background='white')
        style.configure('Title.TLabel', font=(font, 22, 'bold'))
        style.configure('Section.TLabel', font=(font, 11, 'bold'), background='white')
        style.configure('Muted.TLabel', foreground='#596d82')
        style.configure('TButton', padding=(14, 9), background='#e8eef7', foreground='#173957')
        style.configure('Accent.TButton', background='#2465c7', foreground='white')
        style.map('Accent.TButton', background=[('disabled', '#dbe3ef'), ('active', '#1c55aa')],
                  foreground=[('disabled', '#78869b')])
        style.configure('Treeview', rowheight=30, background='white', fieldbackground='white')
        body = ttk.Frame(window, padding=24)
        body.pack(fill='both', expand=True)
        body.columnconfigure(0, weight=1)
        body.rowconfigure(4, weight=1)
        header = ttk.Frame(body)
        header.grid(row=0, column=0, sticky='ew', pady=(0, 18))
        ttk.Label(header, text='Trading Bot', style='Title.TLabel').pack(side='left')
        ttk.Label(header, text='FUTU  /  模拟账户  /  单次执行', style='Muted.TLabel').pack(side='right')
        card = ttk.Frame(body, style='Card.TFrame', padding=18)
        card.grid(row=1, column=0, sticky='ew', pady=(0, 12))
        card.columnconfigure(0, weight=1)
        ttk.Label(card, text='1  连接账户', style='Section.TLabel').grid(row=0, column=0, sticky='w')
        ttk.Label(card, textvariable=self.connection, style='Card.TLabel', wraplength=390).grid(row=1, column=0, sticky='w', pady=(8, 0))
        self.button(card, '检查模拟盘连接', lambda: self.start('status')).grid(row=0, column=1, rowspan=2, padx=(10, 8))
        self.button(card, '打开 Futu OpenD', self.open_opend).grid(row=0, column=2, rowspan=2)
        data = ttk.Frame(body, style='Card.TFrame', padding=18)
        data.grid(row=2, column=0, sticky='ew', pady=(0, 12))
        data.columnconfigure(0, weight=1)
        ttk.Label(data, text='2  更新数据并预览', style='Section.TLabel').grid(row=0, column=0, sticky='w')
        ttk.Label(data, textvariable=self.strategy, style='Card.TLabel').grid(row=0, column=1, columnspan=2, sticky='e')
        selector = ttk.Frame(data, style='Card.TFrame')
        selector.grid(row=1, column=0, columnspan=3, sticky='ew', pady=(12, 0))
        ttk.Label(selector, text='数据源', style='Card.TLabel').pack(side='left', padx=(0, 8))
        self.source_select = ttk.Combobox(selector, textvariable=self.source, values=tuple(SOURCES), state='readonly', width=12)
        self.source_select.pack(side='left')
        self.source_select.bind('<<ComboboxSelected>>', self.change_source)
        self.configure_button = self.button(selector, '配置 Massive', self.configure_massive)
        self.configure_button.pack(side='left', padx=8)
        self.data_status_button = self.button(selector, '检查数据权限', lambda: self.start('data-status'))
        self.data_status_button.pack(side='left')
        download = ttk.Frame(data, style='Card.TFrame')
        download.grid(row=2, column=0, columnspan=3, sticky='ew', pady=(10, 0))
        self.button(download, '下载并预览', lambda: self.start('refresh'), style='Accent.TButton').pack(side='left')
        self.cancel_download_button = ttk.Button(download, text='停止下载', command=self.cancel_download)
        self.cancel_download_button.pack(side='left', padx=8)
        ttk.Label(download, text='首次扫描耗时取决于套餐速度；缓存可复用', style='Card.TLabel').pack(side='left')
        ttk.Entry(data, textvariable=self.snapshot, state='readonly').grid(row=3, column=0, sticky='ew', pady=(12, 0))
        self.button(data, '选择快照…', self.choose).grid(row=3, column=1, padx=8, pady=(12, 0))
        self.preview_button = self.button(data, '预览策略', lambda: self.start('preview'))
        self.preview_button.grid(row=3, column=2, pady=(12, 0))
        ttk.Label(data, textvariable=self.hint, style='Card.TLabel', wraplength=910).grid(row=4, column=0, columnspan=3, sticky='w', pady=(10, 0))
        actions = ttk.Frame(body)
        actions.grid(row=3, column=0, sticky='ew', pady=(0, 14))
        self.execute_button = self.button(actions, '执行一轮模拟交易', lambda: self.start('execute'), style='Accent.TButton')
        self.execute_button.pack(side='left')
        self.button(actions, '运行离线演示', lambda: self.start('demo')).pack(side='left', padx=10)
        self.button(actions, '打开运行记录', self.open_runs).pack(side='right')
        self.tabs = ttk.Notebook(body)
        self.tabs.grid(row=4, column=0, sticky='nsew')
        targets = ttk.Frame(self.tabs, style='Card.TFrame')
        logs = ttk.Frame(self.tabs, style='Card.TFrame')
        self.tabs.add(targets, text='  目标持仓  ')
        self.tabs.add(logs, text='  运行日志  ')
        self.tree = ttk.Treeview(targets, columns=('ticker', 'industry', 'weight', 'score'), show='headings', height=6)
        for key, label in (('ticker', '股票代码'), ('industry', '所属行业'), ('weight', '目标仓位'), ('score', '策略评分')):
            self.tree.heading(key, text=label)
            self.tree.column(key, width=180, anchor='center')
        self.tree.pack(fill='both', expand=True, padx=8, pady=8)
        self.log = ScrolledText(logs, wrap='word', height=9, relief='flat', bg='white', fg='#18324d',
                               font=(font, 10), state='disabled')
        self.log.pack(fill='both', expand=True, padx=8, pady=8)
        footer = ttk.Frame(body)
        footer.grid(row=5, column=0, sticky='ew', pady=(12, 0))
        self.progress = ttk.Progressbar(footer, mode='indeterminate', length=120)
        self.progress.pack(side='right')
        ttk.Label(footer, textvariable=self.state).pack(side='left')
        ttk.Label(body, text='下载数据用于选股研究。模拟交易仍需通过完整行情和账户检查；当前为手动单次运行。',
                  style='Muted.TLabel', wraplength=830).grid(row=6, column=0, sticky='w', pady=(12, 0))
        self.snapshot.trace_add('write', self.invalidate_preview)
        try:
            default = json.loads((self.root / 'configs/data_sources.json').read_text(encoding='utf-8'))['default']
            self.source.set(next(label for label, key in SOURCES.items() if key == default))
        except (OSError, ValueError, KeyError, TypeError, StopIteration):
            pass
        self.change_source()
        self.refresh_controls()
        window.after(100, self.poll)
        if autocheck:
            window.after(150, lambda: self.start('offline'))

    def button(self, parent, text, command, **kwargs):
        button = self.ttk.Button(parent, text=text, command=command, **kwargs)
        self.controls.append(button)
        return button

    def refresh_controls(self):
        for control in self.controls:
            control.configure(state='disabled' if self.busy else 'normal')
        self.preview_button.configure(state='normal' if not self.busy and self.snapshot.get() else 'disabled')
        ready = not self.busy and self.connected and self.preview and self.preview.get('execution_ready')
        self.execute_button.configure(state='normal' if ready else 'disabled')
        self.source_select.configure(state='disabled' if self.busy else 'readonly')
        massive = self.source.get() == 'Massive'
        self.configure_button.configure(state='normal' if not self.busy and massive else 'disabled')
        self.data_status_button.configure(state='normal' if not self.busy and massive else 'disabled')
        self.cancel_download_button.configure(state='normal' if self.busy and massive and self.active_action == 'refresh' and self.can_cancel_download else 'disabled')

    def source_directory(self):
        raw = json.loads((self.root / 'configs/data_sources.json').read_text(encoding='utf-8'))
        path = (self.root / raw['sources'][SOURCES[self.source.get()]]['cache_dir']).resolve()
        if not path.is_relative_to((self.root / 'runs/data').resolve()):
            raise ValueError('数据目录配置无效。')
        return path.parent

    def change_source(self, *_):
        self.snapshot.set('')
        try:
            directory = self.source_directory()
            latest = json.loads((directory / 'latest.json').read_text(encoding='utf-8'))
            path = Path(latest['path']).resolve()
            if path.is_file() and path.is_relative_to(directory):
                self.snapshot.set(str(path))
        except (OSError, ValueError, KeyError, TypeError):
            pass
        self.hint.set('Massive：先配置密钥并检查日线、财报权限。下载后可预览选股；完整扫描可以停止后继续。'
                      if self.source.get() == 'Massive' else 'Futu：保持 OpenD 登录，下载当前科技股数据并预览。')
        self.refresh_controls()

    def configure_massive(self):
        if self.busy:
            return
        dialog = self.tk.Toplevel(self.window)
        dialog.title('配置 Massive 数据源')
        dialog.transient(self.window)
        dialog.grab_set()
        frame = self.ttk.Frame(dialog, padding=20)
        frame.pack(fill='both', expand=True)
        self.ttk.Label(frame, text='API Key（已配置时留空可保留原密钥）').grid(row=0, column=0, columnspan=2, sticky='w')
        key = self.tk.StringVar()
        entry = self.ttk.Entry(frame, textvariable=key, show='●', width=48)
        entry.grid(row=1, column=0, columnspan=2, pady=(6, 14), sticky='ew')
        self.ttk.Label(frame, text='请求速度（次/分钟，按套餐填写）').grid(row=2, column=0, sticky='w')
        rate = self.tk.StringVar(value='5')
        self.ttk.Entry(frame, textvariable=rate, width=8).grid(row=2, column=1, sticky='e')
        self.ttk.Label(frame, text='默认 5 次/分钟。全市场行业扫描可能很慢；付费套餐可按权限提高。\n'
                       '密钥保存在本机 secrets/massive，不会进入代码仓库或运行日志。',
                       wraplength=490).grid(row=3, column=0, columnspan=2, pady=14, sticky='w')

        def save():
            try:
                speed = int(rate.get())
                if not 1 <= speed <= 600:
                    raise ValueError()
            except ValueError:
                self.messagebox.showerror('速度无效', '请输入 1–600 的整数。', parent=dialog)
                return
            credentials = {'api_key': key.get(), 'requests_per_minute': speed}
            key.set('')
            dialog.destroy()
            self.start('configure-data', credentials=credentials)

        self.ttk.Button(frame, text='保存到本机', command=save).grid(row=4, column=1, sticky='e')
        entry.focus_set()

    def cancel_download(self):
        if not (self.busy and self.active_action == 'refresh' and self.source.get() == 'Massive'):
            return
        try:
            path = self.source_directory() / 'cache/cancel.request'
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()
            self.can_cancel_download = False
            self.state.set('正在停止下载，等待当前只读请求返回…')
            self.cancel_download_button.configure(state='disabled')
        except (OSError, ValueError, KeyError):
            self.messagebox.showerror('无法停止', '停止标记无法写入，请等待下载返回。', parent=self.window)

    def invalidate_preview(self, *_):
        self.preview = None
        self.tree.delete(*self.tree.get_children())
        self.hint.set('请预览所选数据；切换数据后需要重新预览。')
        self.refresh_controls()

    def choose(self):
        path = self.filedialog.askopenfilename(parent=self.window, title='选择完整行情快照',
                                               initialdir=self.root / 'runs', filetypes=[('JSON 快照', '*.json')])
        if path:
            self.snapshot.set(path)

    def append_log(self, text):
        self.log.configure(state='normal')
        self.log.insert('end', text + '\n')
        self.log.see('end')
        self.log.configure(state='disabled')

    def start(self, action, *, credentials=None):
        if self.busy:
            return
        token = self.preview.get('preview_token') if self.preview else None
        if action == 'execute' and not (self.connected and token and self.preview.get('execution_ready')):
            return
        path = self.snapshot.get() or None
        self.busy = True
        self.active_action = action
        self.can_cancel_download = False
        if action in {'preview', 'execute', 'demo', 'refresh'}:
            self.preview = None
        if action in {'preview', 'demo', 'refresh'}:
            self.tree.delete(*self.tree.get_children())
        self.state.set(LABELS[action] + '…')
        self.append_log(datetime.now().strftime('%H:%M:%S') + '  ' + LABELS[action])
        self.tabs.select(1)
        self.progress.start(12)
        self.refresh_controls()
        progress = lambda message: self.events.put({'launcher_progress': 1, 'message': message})
        source = SOURCES[self.source.get()]
        threading.Thread(target=lambda: self.events.put(run_worker(self.root, action, path, token, progress,
                          data_source=source, credentials=credentials)), daemon=False).start()

    def poll(self):
        try:
            result = self.events.get_nowait()
        except queue.Empty:
            pass
        else:
            if result.get('launcher_progress') == 1:
                self.state.set(result['message'])
                self.append_log(result['message'])
                if self.active_action == 'refresh' and self.source.get() == 'Massive':
                    # The first progress event is emitted after clearing an old
                    # cancellation marker and acquiring the collection lock.
                    try:
                        self.can_cancel_download = not (self.source_directory() / 'cache/cancel.request').exists()
                    except (OSError, KeyError, ValueError):
                        self.can_cancel_download = False
                    self.refresh_controls()
            else:
                self.busy = False
                self.active_action = None
                self.progress.stop()
                self.accept_result(result)
                self.refresh_controls()
        self.window.after(100, self.poll)

    def accept_result(self, result):
        action, status = result['action'], result['status']
        message = result['message']
        self.state.set('已暂停，请查看日志' if status in {'paused', 'blocked'} else
                       '预览完成' if action in {'preview', 'refresh'} else '本次操作完成')
        if result.get('strategy'):
            self.strategy.set('策略：' + result['strategy'])
        if action in {'offline', 'status'}:
            self.connected = status == 'connected'
            details = result.get('details', {})
            text = '模拟盘已连接 · USD ' + details.get('cash', '—') if self.connected else message
            if self.connected:
                text += ' · 挂单 ' + str(details.get('open_order_count', 0))
            self.connection.set(text)
        if action in {'preview', 'refresh'}:
            if action == 'refresh' and result.get('snapshot_path'):
                self.snapshot.set(result['snapshot_path'])
            self.preview = result if status == 'previewed' else None
            details = result.get('details', {})
            if details.get('execution_allowed') is False:
                self.hint.set(f"{message} 价格截至 {details.get('price_as_of', '—')}；缺失 {details.get('data_issue_count', 0)} 只。"
                              '研究数据没有实时执行报价；来源及价格口径见日志。')
            else:
                self.hint.set(' '.join(result.get('issues', [])) or
                              ('预览完成；检查模拟盘连接后，可手动执行一轮。' if self.preview else message))
        if action == 'execute':
            self.preview = None
            self.connected = False
            self.connection.set('请重新检查账户状态')
            self.hint.set('请核对运行日志与挂单状态；程序不会自动重试，也不会自动撤单。')
        if 'targets' in result:
            self.tree.delete(*self.tree.get_children())
            for target in result['targets']:
                self.tree.insert('', 'end', values=(target.get('ticker', target['asset_id']),
                                                   target.get('industry', '—'),
                                                   f"{target['weight']:.1%}", f"{target['score']:.4f}"))
            self.tabs.select(0)
        if action == 'demo':
            self.hint.set('离线演示使用合成数据，仅展示流程。模拟盘交易仍需选择真实行情快照。')
        display = {k: v for k, v in result.items() if k not in {'preview_token', 'launcher_protocol'}}
        self.append_log(json.dumps(display, ensure_ascii=False, indent=2))
        try:
            out = self.root / 'runs/launcher'
            out.mkdir(parents=True, exist_ok=True)
            name = datetime.now().strftime('%Y%m%d-%H%M%S-') + action + '-' + uuid4().hex[:6] + '.json'
            (out / name).write_text(json.dumps(display, ensure_ascii=False, indent=2), encoding='utf-8')
        except OSError:
            self.append_log('运行结果无法写入文件，请保留窗口中的日志。')

    def open_opend(self):
        path = Path(os.environ.get('LOCALAPPDATA', '')) / 'Programs/FutuOpenD/Futu_OpenD.exe'
        if not path.is_file():
            self.messagebox.showinfo('未找到 OpenD', '请手动打开已安装的 Futu OpenD 并登录，然后检查连接。', parent=self.window)
            return
        try:
            with external_dll_search():
                subprocess.Popen([str(path)], cwd=path.parent, env=external_environment(), shell=False)
            self.state.set('已打开 OpenD；登录后点击检查模拟盘连接')
        except OSError:
            self.messagebox.showerror('启动失败', '无法打开 OpenD，请手动启动。', parent=self.window)

    def open_runs(self):
        try:
            directory = self.root / 'runs/launcher'
            directory.mkdir(parents=True, exist_ok=True)
            os.startfile(directory)
        except OSError:
            self.messagebox.showerror('打开失败', '请手动打开项目中的 runs/launcher 文件夹。', parent=self.window)

    def close(self):
        if self.busy:
            self.messagebox.showinfo('操作尚未结束', '请等待本次操作返回后再关闭。关闭窗口不能撤销已提交的订单。', parent=self.window)
            return
        self.window.destroy()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--self-test', type=Path, help='Offline frozen GUI/worker check; write a JSON result and exit')
    args = parser.parse_args()
    import tkinter as tk
    from tkinter import messagebox
    window = tk.Tk()
    root = project_root()
    if args.self_test:
        window.withdraw()
        app = Launcher(window, root, autocheck=False)
        window.update_idletasks()
        result = run_worker(root, 'offline')
        app.accept_result(result)
        window.update_idletasks()
        report = {'gui_created': True, 'title': window.title(), 'frozen': bool(getattr(sys, 'frozen', False)),
                  'root': str(root), 'worker': result, 'network_requested': False,
                  'data_source': app.source.get(), 'data_source_choices': list(SOURCES),
                  'execute_enabled': str(app.execute_button.cget('state')) != 'disabled'}
        args.self_test.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        window.destroy()
        return 0 if result['status'] == 'ready' else 2
    try:
        with single_instance(root):
            Launcher(window, root)
            window.mainloop()
    except (OSError, ValueError) as exc:
        window.withdraw()
        messagebox.showerror('Trading Bot', str(exc), parent=window)
        window.destroy()
        return 2
    return 0


if __name__ == '__main__':
    from app.portal_launcher import main as portal_main
    raise SystemExit(portal_main())
