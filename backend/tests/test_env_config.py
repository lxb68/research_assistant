from pathlib import Path

import pytest

from app.services.env_config import EnvConfigStore


def test_public_config_never_returns_secret_value(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text("NCBI_API_KEY=super-secret-value\nPORT=4100\n", encoding="utf-8")

    payload = EnvConfigStore(env_path).get_public_config()
    serialized = repr(payload)
    field = next(
        item
        for group in payload["groups"]
        for item in group["fields"]
        if item["key"] == "NCBI_API_KEY"
    )

    assert "super-secret-value" not in serialized
    assert field["configured"] is True
    assert "value" not in field


def test_update_preserves_comments_and_creates_backup(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    original = "# 用户注释\nUNKNOWN_SETTING=keep-me\nPORT=4000\nNCBI_API_KEY=old-secret\n"
    env_path.write_text(original, encoding="utf-8")

    result = EnvConfigStore(env_path).update({"PORT": 4200, "NCBI_API_KEY": "new-secret"})

    updated = env_path.read_text(encoding="utf-8")
    assert "# 用户注释" in updated
    assert "UNKNOWN_SETTING=keep-me" in updated
    assert "PORT=4200" in updated
    assert "NCBI_API_KEY=new-secret" in updated
    assert (tmp_path / ".env.bak").read_text(encoding="utf-8") == original
    assert "new-secret" not in repr(result)


def test_empty_secret_preserves_and_null_clears(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text("NCBI_API_KEY=existing\nPORT=4000\n", encoding="utf-8")
    store = EnvConfigStore(env_path)

    store.update({"NCBI_API_KEY": ""})
    assert "NCBI_API_KEY=existing" in env_path.read_text(encoding="utf-8")

    store.update({"NCBI_API_KEY": None})
    assert "NCBI_API_KEY" not in env_path.read_text(encoding="utf-8")


def test_unknown_and_invalid_values_are_rejected(tmp_path: Path) -> None:
    store = EnvConfigStore(tmp_path / ".env")

    with pytest.raises(ValueError, match="不允许修改"):
        store.update({"DATABASE_URL": "unexpected"})
    with pytest.raises(ValueError, match="监听端口"):
        store.update({"PORT": 70000})
    with pytest.raises(ValueError, match="日志级别"):
        store.update({"LOG_LEVEL": "VERBOSE"})


def test_evidence_budget_fields_expose_distinct_semantics(tmp_path: Path) -> None:
    payload = EnvConfigStore(tmp_path / ".env").get_public_config()
    fields = {
        item["key"]: item
        for group in payload["groups"]
        for item in group["fields"]
    }

    assert fields["RESEARCH_AGENT_MAX_SOURCES"]["label"] == "单路候选证据上限"
    assert "不是最终回答的目标证据数" in fields["RESEARCH_AGENT_MAX_SOURCES"]["description"]
    assert fields["RAG_COMPLEX_TARGET_EVIDENCE"]["label"] == "复杂问题目标证据数"
    assert "不保证取满" in fields["RAG_COMPLEX_TARGET_EVIDENCE"]["description"]
    assert (
        fields["RESEARCH_AGENT_MAX_EVIDENCE_GROUPS"]["label"]
        == "最终证据组安全上限"
    )


def test_evidence_budget_relationships_are_validated_before_persistence(
    tmp_path: Path,
) -> None:
    env_path = tmp_path / ".env"
    store = EnvConfigStore(env_path)

    with pytest.raises(ValueError, match="复杂问题目标证据数不能大于"):
        store.update(
            {
                "RAG_COMPLEX_TARGET_EVIDENCE": 13,
                "RESEARCH_AGENT_MAX_EVIDENCE_GROUPS": 12,
            }
        )

    with pytest.raises(ValueError, match="最低证据数量门槛不能大于"):
        store.update(
            {
                "ORCHESTRATOR_MIN_EVIDENCE": 13,
                "RESEARCH_AGENT_MAX_EVIDENCE_GROUPS": 12,
            }
        )

    assert not env_path.exists()
