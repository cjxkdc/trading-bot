"""Optional strategy documentation, confined to the strategy module directory."""
from pathlib import Path, PurePosixPath

MAX_README_BYTES = 256 * 1024


def strategy_documentation(directory, reference):
    if reference is None:
        return {'readme': None}
    if (not isinstance(reference, str) or not reference or len(reference) > 1024
            or '\\' in reference or ':' in reference or '\0' in reference
            or PurePosixPath(reference).is_absolute()
            or '..' in reference.split('/') or Path(reference).suffix.lower() != '.md'):
        return {'readme': None, 'readme_issue': '说明路径无效，请使用策略目录内的 Markdown 文件。'}
    try:
        root = Path(directory).resolve()
        path = (root / reference).resolve()
        if not path.is_relative_to(root):
            return {'readme': None, 'readme_issue': '说明文件必须位于策略目录内。'}
        with path.open('rb') as stream:
            content = stream.read(MAX_README_BYTES + 1)
        if len(content) > MAX_README_BYTES:
            return {'readme': None, 'readme_issue': '说明文件过大，请控制在 256 KB 以内。'}
        text = content.decode('utf-8-sig').replace('\r\n', '\n').replace('\r', '\n')
        if '\0' in text:
            raise UnicodeError('binary documentation')
    except (OSError, ValueError, RuntimeError, UnicodeError):
        return {'readme': None, 'readme_issue': '说明暂时无法读取，请检查文件和 UTF-8 编码。'}
    return {'readme': {'path': reference, 'content': text}}
