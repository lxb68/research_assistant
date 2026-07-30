"""验证 Prompt 资源集中加载、模板渲染和目录边界。"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.prompt_loader import PROMPT_ROOT, load_prompt, render_prompt


def test_prompt_root_replaces_legacy_src_directory() -> None:
    backend_root = Path(__file__).resolve().parents[1]

    assert PROMPT_ROOT == backend_root / "prompt"
    assert PROMPT_ROOT.is_dir()
    assert not (backend_root / "src").exists()


def test_load_prompt_reads_utf8_resource() -> None:
    prompt = load_prompt("research/answer.zh.md")

    assert "知识库证据" in prompt
    assert "{{evidence}}" in prompt
    assert "不以寒暄、确认收到、任务复述、输入摘要、处理过程" in prompt
    assert "首句必须直接提供结论、判断或有信息量的结构概括" in prompt


def test_render_prompt_replaces_declared_values() -> None:
    prompt = render_prompt(
        "semantic_graph/type_correction.zh.md",
        invalid_types='["Model"]',
    )

    assert '["Model"]' in prompt
    assert "{{invalid_types}}" not in prompt


def test_render_prompt_rejects_missing_values() -> None:
    with pytest.raises(ValueError, match="invalid_types"):
        render_prompt("semantic_graph/type_correction.zh.md")


@pytest.mark.parametrize(
    "resource",
    ["../secret.md", "/absolute.md", "research/answer.txt", ""],
)
def test_load_prompt_rejects_paths_outside_catalog(resource: str) -> None:
    with pytest.raises(ValueError, match="Prompt 资源路径无效"):
        load_prompt(resource)


def test_all_prompt_resources_are_utf8_and_non_empty() -> None:
    resources = sorted(PROMPT_ROOT.rglob("*.md"))

    assert resources
    for resource in resources:
        assert resource.read_text(encoding="utf-8").strip(), resource
