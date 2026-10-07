"""Owner-managed strategy source files and portable plugin bundles."""
import ast
from hashlib import sha256
import importlib
import io
import json
from pathlib import Path
import re
import sys
from uuid import uuid4
import zipfile

from app.control_contracts import ControlError, project_path

MAX_CODE = 512 * 1024
MAX_BUNDLE = 1024 * 1024


class StrategyFiles:
    def __init__(self, root, registry):
        self.root, self.registry = Path(root).resolve(), registry

    def location(self, strategy_type):
        if not isinstance(strategy_type, str) or not re.fullmatch(r'[a-z][a-z0-9_]*', strategy_type):
            raise ControlError('策略模块编号无效。')
        entry = self.registry.catalog('strategy').get(strategy_type)
        if entry is None:
            raise ControlError('策略模块未登记。')
        module, name = entry['entrypoint'].split(':')
        return entry, project_path(self.root, module.replace('.', '/') + '.py', under='strategies'), module, name

    def read(self, strategy_type):
        entry, path, module, name = self.location(strategy_type)
        raw = path.read_bytes()
        if len(raw) > MAX_CODE:
            raise ControlError('策略源文件超过 512 KB。')
        return {'filename': path.relative_to(self.root).as_posix(), 'content': raw.decode('utf-8-sig'),
                'revision': sha256(raw).hexdigest()}

    def syntax(self, text, class_name):
        if not isinstance(text, str) or len(text.encode('utf-8')) > MAX_CODE or '\0' in text:
            raise ControlError('策略代码须为 512 KB 以内的 UTF-8 文本。')
        try:
            tree = ast.parse(text)
            compile(tree, '<strategy>', 'exec')
        except SyntaxError as exc:
            raise ControlError(f'Python 语法错误，请检查第 {exc.lineno} 行。') from None
        if not any(isinstance(node, ast.ClassDef) and node.name == class_name for node in tree.body):
            raise ControlError('代码必须保留登记的策略类名。')

    def write(self, strategy_type, value):
        if not isinstance(value, dict) or set(value) != {'content', 'revision'}:
            raise ControlError('保存代码需要内容与当前文件版本。')
        entry, path, module, name = self.location(strategy_type)
        original = path.read_bytes()
        if sha256(original).hexdigest() != value['revision']:
            raise ControlError('代码已被其他窗口修改，请重新打开。')
        self.syntax(value['content'], name)
        backup = self.root / 'runs/strategy_manager/source-history' / (uuid4().hex + '.py')
        backup.parent.mkdir(parents=True, exist_ok=True)
        backup.write_bytes(original)
        temporary = path.with_name('.' + uuid4().hex + '.tmp')
        try:
            temporary.write_bytes(value['content'].encode('utf-8'))
            if sha256(path.read_bytes()).hexdigest() != value['revision']:
                raise ControlError('保存前代码已变化，请重新加载。')
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
        sys.modules.pop(module, None)
        cache = path.parent / '__pycache__'
        for cached in cache.glob(path.stem + '.*.pyc'):
            if cached.resolve().is_relative_to(self.root):
                cached.unlink(missing_ok=True)
        importlib.invalidate_caches()
        return {'message': '代码已保存，原版本已备份；请重新回测。', 'revision': sha256(path.read_bytes()).hexdigest()}

    def install(self, content):
        if not isinstance(content, bytes) or len(content) > MAX_BUNDLE:
            raise ControlError('策略 ZIP 包不能超过 1 MB。')
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as bundle:
                names = bundle.namelist()
                if len(set(names)) != len(names) or not {'strategy.py', 'manifest.json'} <= set(names) or set(names) - {'strategy.py', 'manifest.json', 'README.md'}:
                    raise ValueError()
                if any(info.file_size > MAX_CODE for info in bundle.infolist()):
                    raise ValueError()
                code = bundle.read('strategy.py').decode('utf-8-sig')
                entry = json.loads(bundle.read('manifest.json').decode('utf-8-sig'))
                readme = bundle.read('README.md').decode('utf-8-sig') if 'README.md' in names else None
            ident = entry['type']
            if not re.fullmatch(r'[a-z][a-z0-9_]*', ident) or entry['kind'] != 'portfolio':
                raise ValueError()
            class_name = entry['entrypoint'].split(':')[1]
            if not class_name.isidentifier():
                raise ValueError()
            self.syntax(code, class_name)
            if ident in self.registry.catalog('strategy'):
                raise ControlError('策略编号已存在，请换一个编号；原策略不会被覆盖。')
            from framework.data_requirements import declared_requirements
            from framework.portfolio import HistoryRequirements
            conditional = entry.get('data_requirements', {}).get('conditional_fields', [])
            parameters = {r['parameter']: r['equals'] for r in conditional}
            declared_requirements(entry.get('data_requirements'), entry['input_schema'], HistoryRequirements(1, 1), parameters)
            suffix = 'user_' + ident + '_' + uuid4().hex[:8]
            entry['entrypoint'] = 'strategies.' + suffix + ':' + class_name
            entry.pop('readme', None)
            if readme is not None:
                entry['readme'] = suffix + '.md'
            path = self.root / 'strategies/plugins' / (ident + '.json')
            source = self.root / 'strategies' / (suffix + '.py')
            documentation = self.root / 'strategies' / (suffix + '.md')
            if path.exists():
                raise ControlError('同名登记文件已存在，请先检查。')
            from app.catalog import scan_catalog
            check = self.root / 'runs/strategy_manager/upload-check' / uuid4().hex
            check.mkdir(parents=True)
            draft = check / 'manifest.json'
            draft.write_text(json.dumps(entry, ensure_ascii=False), encoding='utf-8')
            if ident not in scan_catalog(check, 'strategies', {'portfolio'}):
                raise ValueError()
            source.write_bytes(code.encode('utf-8'))
            if readme is not None:
                documentation.write_text(readme, encoding='utf-8')
            path.write_text(json.dumps(entry, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
            importlib.invalidate_caches()
            return {'message': '策略插件已上传，现可创建运行实例。', 'type': ident}
        except ControlError:
            raise
        except (OSError, ValueError, KeyError, TypeError, IndexError, UnicodeError, zipfile.BadZipFile):
            raise ControlError('策略包格式无效：需要 strategy.py、manifest.json，可选 README.md。') from None

    def bundle(self, strategy_type):
        entry, path, module, name = self.location(strategy_type)
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, 'w', zipfile.ZIP_DEFLATED) as package:
            package.writestr('strategy.py', path.read_bytes())
            package.writestr('manifest.json', json.dumps(entry, ensure_ascii=False, indent=2))
            reference = entry.get('readme')
            if reference:
                from app.strategy_documentation import strategy_documentation
                info = strategy_documentation(self.root / 'strategies', reference).get('readme')
                if info:
                    package.writestr('README.md', info['content'])
        return stream.getvalue()
