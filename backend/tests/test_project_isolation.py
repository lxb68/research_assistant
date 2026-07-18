"""验证项目论文、领域树输入和知识图谱目录不会跨项目串用。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.agents.domainTree_agent import DomainTreeAgent
from app.services.paper_repository import PaperRepository
from app.services.domain_tree_store import DomainTreeStore
from app.services.project_repository import DEFAULT_PROJECT_ID, ProjectRepository
from app.services.project_scope import ProjectScopeService


class ProjectIsolationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.database = self.root / "papers.sqlite3"
        self.papers = PaperRepository(self.database)
        self._save_paper("paper-a", "领域 A 论文")
        self._save_paper("paper-b", "领域 B 论文")
        self.projects = ProjectRepository(self.database)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _save_paper(self, record_id: str, title: str) -> None:
        markdown_dir = self.root / "markdown" / record_id
        markdown_dir.mkdir(parents=True)
        markdown_path = markdown_dir / "full.md"
        markdown_path.write_text(f"# {title}\n\n## 方法\n\n测试正文", encoding="utf-8")
        self.papers.save(
            {
                "id": record_id,
                "source": "test",
                "title": title,
                "markdownPath": str(markdown_path),
                "markdownOutputDir": str(markdown_dir),
            },
        )

    def test_default_project_inherits_existing_global_papers(self) -> None:
        self.assertEqual(
            set(self.projects.list_paper_ids(DEFAULT_PROJECT_ID)),
            {"paper-a", "paper-b"},
        )

    def test_projects_keep_independent_paper_memberships_and_analysis_paths(self) -> None:
        project_a = self.projects.create(name="项目 A", paper_ids=["paper-a"])
        project_b = self.projects.create(name="项目 B", paper_ids=["paper-b"])
        agent = DomainTreeAgent(
            storage_dir=self.root,
            metadata_db_path=self.database,
            project_repository=self.projects,
        )

        documents_a = agent._load_documents(project_a["id"])
        documents_b = agent._load_documents(project_b["id"])

        self.assertEqual([document.record_id for document in documents_a], ["paper-a"])
        self.assertEqual([document.record_id for document in documents_b], ["paper-b"])
        self.assertNotEqual(agent._analysis_dir(project_a["id"]), agent._analysis_dir(project_b["id"]))
        self.assertEqual(agent._load_documents("missing-project"), [])

    def test_updating_one_project_does_not_change_another(self) -> None:
        project_a = self.projects.create(name="项目 A", paper_ids=["paper-a"])
        project_b = self.projects.create(name="项目 B", paper_ids=["paper-b"])

        self.projects.replace_papers(project_a["id"], ["paper-a", "paper-b"])

        self.assertEqual(set(self.projects.list_paper_ids(project_a["id"])), {"paper-a", "paper-b"})
        self.assertEqual(self.projects.list_paper_ids(project_b["id"]), ["paper-b"])

    def test_store_rejects_artifacts_claiming_another_project(self) -> None:
        output_dir = self.root / "domain_tree" / "project-a"
        output_dir.mkdir(parents=True)
        (output_dir / "domain_tree.json").write_text(
            json.dumps({"projectId": "project-b", "domainTree": [], "graphStatus": "ready"}),
            encoding="utf-8",
        )

        self.assertIsNone(DomainTreeStore().load_result(output_dir, "project-a"))

    def test_research_scope_filters_history_and_rejects_outside_papers(self) -> None:
        project = self.projects.create(name="项目 A", paper_ids=["paper-a"])
        scope = ProjectScopeService(self.database)
        arguments = scope.build_research_arguments(
            project_id=project["id"],
            requested_paper_ids=[],
            history=[
                {
                    "role": "assistant",
                    "content": "历史回答",
                    "sources": [
                        {"record_id": "paper-a", "index": 1},
                        {"record_id": "paper-b", "index": 2},
                    ],
                },
            ],
        )

        self.assertEqual(arguments["paper_ids"], ["paper-a"])
        self.assertEqual(arguments["history"][0]["sources"], [{"record_id": "paper-a", "index": 1}])
        with self.assertRaises(ValueError):
            scope.build_research_arguments(
                project_id=project["id"],
                requested_paper_ids=["paper-b"],
                history=[],
            )


if __name__ == "__main__":
    unittest.main()
