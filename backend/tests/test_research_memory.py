"""研究记忆与全局偏好服务的隔离、持久化和提炼回退测试。"""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes import memory as memory_routes
from app.services.answer_policy import AnswerPolicy
from app.services.research_memory import ResearchMemoryExtractor, ResearchMemoryStore


def test_memory_store_keeps_projects_isolated(tmp_path):
    store = ResearchMemoryStore(tmp_path / "memory.sqlite3")
    first = store.create({
        "projectId": "project-a",
        "projectName": "A",
        "type": "conclusion",
        "title": "结论 A",
        "summary": "只属于项目 A",
        "tags": ["RAG"],
        "confidence": 0.8,
        "evidence": [{"recordId": "paper-1"}],
    })
    store.create({
        "projectId": "project-b",
        "type": "fact",
        "title": "事实 B",
        "summary": "只属于项目 B",
    })

    project_a = store.list(project_ids=["project-a"])

    assert [item["id"] for item in project_a] == [first["id"]]
    assert project_a[0]["evidence"] == [{"recordId": "paper-1"}]


def test_preferences_are_global_but_not_stored_as_research_memory(tmp_path):
    store = ResearchMemoryStore(tmp_path / "memory.sqlite3")

    preferences = store.update_preferences({
        "preferredName": "林老师",
        "answerStyle": "先给结论",
    })

    assert preferences["preferredName"] == "林老师"
    assert store.list() == []
    context = store.build_response_context(project_ids=["project-a"])
    assert "林老师" in context
    assert "projectMemories" not in context


def test_fallback_extractor_summarizes_instead_of_copying_full_answer(monkeypatch):
    monkeypatch.setattr(
        "app.services.research_memory.ModelConfigStore.build_model_payload",
        lambda self: None,
    )
    answer = "第一条关键结论。\n\n第二条限定条件。\n\n第三条补充说明。\n\n不应完整保留的第四段。"

    candidate = ResearchMemoryExtractor().extract("应该采用什么方案？", answer, [])

    assert candidate["type"] == "decision"
    assert "第一条关键结论" in candidate["summary"]
    assert "不应完整保留的第四段" not in candidate["summary"]
    assert candidate["summary"] != answer


def test_memory_api_supports_confirm_edit_and_delete(monkeypatch, tmp_path):
    store = ResearchMemoryStore(tmp_path / "memory.sqlite3")
    monkeypatch.setattr(memory_routes, "research_memory_store", store)
    app = FastAPI()
    app.include_router(memory_routes.router)
    client = TestClient(app)

    created = client.post("/api/research-memories", json={
        "projectId": "project-a",
        "type": "decision",
        "title": "采用语义分块",
        "summary": "优先按章节语义分块。",
        "tags": ["RAG"],
        "confidence": 0.75,
    })
    assert created.status_code == 201
    memory_id = created.json()["memory"]["id"]

    edited = client.patch(f"/api/research-memories/{memory_id}", json={
        "summary": "优先按章节语义分块，并保留标题路径。",
    })
    assert edited.status_code == 200
    assert "标题路径" in edited.json()["memory"]["summary"]

    assert client.delete(f"/api/research-memories/{memory_id}").status_code == 204
    assert client.get("/api/research-memories").json()["count"] == 0


def test_answer_policy_marks_memory_as_non_evidence_context():
    prompt = AnswerPolicy().build_prompt(
        base_prompt="{{evidence}}",
        evidence_context="论文证据",
        answer_requirements=[],
        retrieval_state={},
        response_context='{"userPreferences":{"preferredName":"林老师"},"projectMemories":[]}',
    )

    assert "林老师" in prompt
    assert "不能替代检索证据" in prompt
