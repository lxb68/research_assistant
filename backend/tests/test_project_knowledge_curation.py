"""验证领域树和知识图谱人工修订的一致性边界。"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.services.domain_tree_store import DomainTreeStore
from app.services.project_knowledge import (
    KnowledgeCurationError,
    KnowledgeRevisionConflict,
    ProjectKnowledgeService,
)


class ProjectKnowledgeCurationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.output_dir = Path(self.temp_dir.name)
        self.project_id = "project-a"
        (self.output_dir / "domain_tree.json").write_text(
            json.dumps(
                {
                    "projectId": self.project_id,
                    "graphStatus": "ready",
                    "domainTree": [
                        {"label": "人工智能", "child": [{"label": "机器学习"}]},
                        {"label": "数据库"},
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        (self.output_dir / "knowledge_graph.json").write_text(
            json.dumps(
                {
                    "projectId": self.project_id,
                    "nodes": [
                        {"id": f"project:{self.project_id}", "name": self.project_id, "type": "project"},
                        {"id": "domain:1", "name": "人工智能", "type": "domain"},
                        {"id": "domain:1.1", "name": "机器学习", "type": "subdomain"},
                        {"id": "entity:a", "name": "方法 A", "type": "entity"},
                        {"id": "entity:b", "name": "数据集 B", "type": "entity"},
                    ],
                    "edges": [
                        {"source": f"project:{self.project_id}", "target": "domain:1", "relation": "has_domain"},
                        {"source": "domain:1", "target": "domain:1.1", "relation": "has_subdomain"},
                        {"source": "entity:a", "target": "entity:b", "relation": "semantic_relation"},
                    ],
                    "entities": [
                        {"id": "entity:a", "name": "方法 A", "type": "方法"},
                        {"id": "entity:b", "name": "数据集 B", "type": "数据集"},
                    ],
                    "semanticRelations": [
                        {
                            "id": "relation:ab",
                            "source": "entity:a",
                            "target": "entity:b",
                            "predicate": "应用于",
                            "relationType": "experimental",
                            "confidence": 0.8,
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        self.service = ProjectKnowledgeService(self.output_dir, self.project_id)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_tree_update_uses_stable_id_without_overwriting_generated_file(self) -> None:
        initial = self.service.get_result()
        node_id = initial["domainTree"][0]["id"]
        response = self.service.update_tree_node(node_id, {"label": "智能系统"}, 0)

        self.assertEqual(response["domainTree"][0]["label"], "智能系统")
        self.assertEqual(response["knowledgeGraph"]["nodes"][1]["name"], "智能系统")
        raw = DomainTreeStore().load_raw_result(self.output_dir, self.project_id)
        self.assertEqual(raw["domainTree"][0]["label"], "人工智能")
        self.assertEqual(response["curation"]["revision"], 1)

    def test_tree_delete_preview_and_cascade_are_recoverable(self) -> None:
        initial = self.service.get_result()
        node_id = initial["domainTree"][0]["id"]
        preview = self.service.delete_tree_node(node_id, 0, dry_run=True)
        self.assertEqual(preview["impact"]["descendantCount"], 1)
        self.assertEqual(self.service.get_result()["curation"]["revision"], 0)

        deleted = self.service.delete_tree_node(node_id, 0)
        self.assertEqual([item["label"] for item in deleted["domainTree"]], ["数据库"])
        restored = self.service.restore_tree_node(node_id, 1)
        self.assertEqual(len(restored["domainTree"]), 2)

    def test_entity_delete_removes_incident_relations_from_both_graph_views(self) -> None:
        preview = self.service.delete_entity("entity:a", 0, dry_run=True)
        self.assertEqual(preview["impact"]["relationCount"], 1)
        deleted = self.service.delete_entity("entity:a", 0)
        graph = deleted["knowledgeGraph"]
        self.assertNotIn("entity:a", {item["id"] for item in graph["entities"]})
        self.assertEqual(graph["semanticRelations"], [])
        self.assertFalse(any(edge.get("source") == "entity:a" for edge in graph["edges"]))

        restored = self.service.restore_entity("entity:a", 1)
        self.assertEqual(len(restored["knowledgeGraph"]["semanticRelations"]), 1)

    def test_relation_update_validates_endpoints_and_rebuilds_generic_edge(self) -> None:
        with self.assertRaises(KnowledgeCurationError):
            self.service.update_relation("relation:ab", {"source": "entity:missing"}, 0)

        updated = self.service.update_relation(
            "relation:ab",
            {"predicate": "评估于", "confidence": 0.95},
            0,
        )
        relation = updated["knowledgeGraph"]["semanticRelations"][0]
        edge = next(item for item in updated["knowledgeGraph"]["edges"] if item["relation"] == "semantic_relation")
        self.assertEqual(relation["predicate"], "评估于")
        self.assertEqual(edge["semanticRelationId"], "relation:ab")
        self.assertEqual(edge["confidence"], 0.95)

    def test_rejects_stale_revision(self) -> None:
        self.service.update_entity("entity:a", {"name": "方法 Alpha"}, 0)
        with self.assertRaises(KnowledgeRevisionConflict):
            self.service.update_entity("entity:b", {"name": "数据集 Beta"}, 0)

    def test_building_snapshot_does_not_fabricate_graph_nodes(self) -> None:
        (self.output_dir / "domain_tree.json").write_text(
            json.dumps(
                {
                    "projectId": self.project_id,
                    "graphStatus": "building",
                    "domainTree": [{"label": "人工智能"}],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        result = self.service.get_result()
        self.assertTrue(result["domainTree"][0]["id"].startswith("tree:"))
        self.assertEqual(result["knowledgeGraph"], {})

    def test_legacy_generic_graph_keeps_semantic_edges(self) -> None:
        graph_path = self.output_dir / "knowledge_graph.json"
        graph = json.loads(graph_path.read_text(encoding="utf-8"))
        graph.pop("entities")
        graph.pop("semanticRelations")
        graph_path.write_text(json.dumps(graph, ensure_ascii=False), encoding="utf-8")

        result = self.service.get_result()["knowledgeGraph"]
        self.assertIn("entity:a", {item["id"] for item in result["nodes"]})
        self.assertTrue(any(item.get("relation") == "semantic_relation" for item in result["edges"]))


if __name__ == "__main__":
    unittest.main()
