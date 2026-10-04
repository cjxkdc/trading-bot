"""Provider-neutral contracts used by the local control plane."""
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class ControlError(ValueError):
    """A credential-free, user-facing control-plane error."""


def project_path(root, value, *, under=None):
    root = Path(root).resolve()
    if not isinstance(value, (str, Path)):
        raise ControlError('模块路径配置无效。')
    path = (root / value).resolve()
    boundary = (root / under).resolve() if under else root
    if not path.is_relative_to(boundary) or path == boundary:
        raise ControlError('模块路径必须位于指定的项目目录内。')
    return path


@dataclass(frozen=True)
class SnapshotInput:
    schema: str
    research_only: bool
    raw: dict
    stocks: tuple
    metadata: dict
    labels: dict
    execution: Any = None
    order_requirements: Any = None
