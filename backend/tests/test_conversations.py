from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes import jobs as jobs_routes
from app.services.conversations import ConversationStore


def test_rename_conversation_persists_title(tmp_path):
    store = ConversationStore(tmp_path / "conversations.sqlite3")
    store.ensure_conversation("conversation-1", title="旧标题")

    assert store.rename("conversation-1", "新标题") is True
    assert store.get("conversation-1")["title"] == "新标题"


def test_rename_missing_conversation_returns_false(tmp_path):
    store = ConversationStore(tmp_path / "conversations.sqlite3")

    assert store.rename("missing", "新标题") is False


def test_delete_conversation_removes_messages(tmp_path):
    database_path = tmp_path / "conversations.sqlite3"
    store = ConversationStore(database_path)
    store.ensure_conversation("conversation-1", title="待删除会话")
    store.upsert_message(
        "conversation-1",
        "message-1",
        role="user",
        content="测试消息",
    )

    assert store.delete("conversation-1") is True
    assert store.get("conversation-1") is None
    with store.connect() as connection:
        message_count = connection.execute(
            "SELECT COUNT(*) FROM conversation_messages WHERE conversation_id = ?",
            ("conversation-1",),
        ).fetchone()[0]
    assert message_count == 0


def test_delete_missing_conversation_returns_false(tmp_path):
    store = ConversationStore(tmp_path / "conversations.sqlite3")

    assert store.delete("missing") is False


def test_delete_conversation_endpoint_returns_204_then_404(monkeypatch, tmp_path):
    store = ConversationStore(tmp_path / "conversations.sqlite3")
    store.ensure_conversation("conversation-1", title="待删除会话")
    monkeypatch.setattr(jobs_routes, "conversation_store", store)
    app = FastAPI()
    app.include_router(jobs_routes.router)
    client = TestClient(app)

    response = client.delete("/api/conversations/conversation-1")

    assert response.status_code == 204
    assert response.content == b""
    assert client.delete("/api/conversations/conversation-1").status_code == 404
