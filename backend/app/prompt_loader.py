"""统一定位和读取后端 Prompt 资源。"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path, PurePosixPath
import re
from typing import Any


PROMPT_ROOT = Path(__file__).resolve().parents[1] / "prompt"
_PLACEHOLDER_PATTERN = re.compile(r"\{\{([A-Za-z_][A-Za-z0-9_]*)\}\}")


@lru_cache(maxsize=128)
def load_prompt(resource: str) -> str:
    """读取 UTF-8 Prompt；resource 必须是 prompt 目录内的相对 .md 路径。"""
    normalized = str(resource or "").strip().replace("\\", "/")
    relative_path = PurePosixPath(normalized)
    if (
        not normalized
        or relative_path.is_absolute()
        or ".." in relative_path.parts
        or relative_path.suffix != ".md"
    ):
        raise ValueError(f"Prompt 资源路径无效：{resource}")

    prompt_path = PROMPT_ROOT.joinpath(*relative_path.parts)
    try:
        return prompt_path.read_text(encoding="utf-8")
    except FileNotFoundError as error:
        raise FileNotFoundError(f"Prompt 资源不存在：{prompt_path}") from error


def render_prompt(resource: str, **values: Any) -> str:
    """渲染显式 ``{{name}}`` 占位符，并拒绝缺失变量。"""
    template = load_prompt(resource)
    required = set(_PLACEHOLDER_PATTERN.findall(template))
    missing = sorted(required.difference(values))
    if missing:
        raise ValueError(f"Prompt 模板缺少变量：{', '.join(missing)}")
    return _PLACEHOLDER_PATTERN.sub(lambda match: str(values[match.group(1)]), template)
