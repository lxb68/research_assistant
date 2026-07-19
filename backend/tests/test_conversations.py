from app.services.conversations import ConversationStore


def test_rename_conversation_persists_title(tmp_path):
    store = ConversationStore(tmp_path / "conversations.sqlite3")
    store.ensure_conversation("conversation-1", title="旧标题")

    assert store.rename("conversation-1", "新标题") is True
    assert store.get("conversation-1")["title"] == "新标题"


def test_rename_missing_conversation_returns_false(tmp_path):
    store = ConversationStore(tmp_path / "conversations.sqlite3")

    assert store.rename("missing", "新标题") is False
