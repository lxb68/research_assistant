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

