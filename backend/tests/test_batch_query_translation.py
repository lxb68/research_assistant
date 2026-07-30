"""验证检索分面使用单次批量翻译并保持失败回退契约。"""

from __future__ import annotations

import json
from unittest.mock import Mock, patch

from app.agents.hunter_agent import HunterAgent


def _agent() -> HunterAgent:
    agent = HunterAgent.__new__(HunterAgent)
    agent.translation_cache = {}
    agent.logs = []
    agent.log_callback = None
    return agent


@patch("app.agents.hunter_agent.ModelConfigStore")
@patch("app.agents.hunter_agent.chat_completion")
def test_batch_translation_uses_one_model_call(
    completion: Mock,
    config_store: Mock,
) -> None:
    config_store.return_value.build_auxiliary_model_payload.return_value = {
        "model": "fast-model"
    }
    completion.return_value = json.dumps(
        {
            "translations": [
                {"id": "0", "query": "decision tree training"},
                {"id": "1", "query": "private tree inference"},
            ]
        }
    )

    translated = _agent().translate_search_queries(
        ["决策树训练", "隐私树推理", "already english"]
    )

    assert translated == [
        "decision tree training",
        "private tree inference",
        "already english",
    ]
    assert completion.call_count == 1
    assert completion.call_args.kwargs["thinking"] is False


@patch("app.agents.hunter_agent.ModelConfigStore")
@patch("app.agents.hunter_agent.chat_completion")
def test_batch_translation_failure_keeps_original_queries(
    completion: Mock,
    config_store: Mock,
) -> None:
    config_store.return_value.build_auxiliary_model_payload.return_value = {
        "model": "fast-model"
    }
    completion.side_effect = RuntimeError("上游暂不可用")

    queries = ["决策树训练", "隐私树推理"]

    assert _agent().translate_search_queries(queries) == queries
    assert completion.call_count == 1
