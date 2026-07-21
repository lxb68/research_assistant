"""领域树与知识图谱人工修订、级联校验和有效结果投影。"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import threading
from typing import Any

from app.services.domain_tree_store import DomainTreeStore


class KnowledgeCurationError(ValueError):
    """人工修订请求不合法。"""


class KnowledgeNotFoundError(KnowledgeCurationError):
    """目标领域节点、实体或关系不存在。"""


class KnowledgeRevisionConflict(KnowledgeCurationError):
    """客户端修订版本已经过期。"""


_LOCKS_GUARD = threading.Lock()
_LOCKS: dict[str, threading.RLock] = {}


def _lock_for(output_dir: Path) -> threading.RLock:
    key = str(output_dir.resolve())
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(key, threading.RLock())


def _stable_tree_id(project_id: str, parent_id: str, label: str, occurrence: int) -> str:
    seed = f"{project_id}\0{parent_id}\0{label.strip().casefold()}\0{occurrence}"
    return f"tree:{hashlib.sha256(seed.encode('utf-8')).hexdigest()[:20]}"


def _normalize_curation(payload: dict[str, Any] | None) -> dict[str, Any]:
    source = payload if isinstance(payload, dict) else {}
    return {
        "schemaVersion": 1,
        "revision": max(0, int(source.get("revision") or 0)),
        "updatedAt": str(source.get("updatedAt") or ""),
        "treeNodePatches": dict(source.get("treeNodePatches") or {}),
        "deletedTreeNodeIds": list(dict.fromkeys(source.get("deletedTreeNodeIds") or [])),
        "entityPatches": dict(source.get("entityPatches") or {}),
        "deletedEntityIds": list(dict.fromkeys(source.get("deletedEntityIds") or [])),
        "relationPatches": dict(source.get("relationPatches") or {}),
        "deletedRelationIds": list(dict.fromkeys(source.get("deletedRelationIds") or [])),
    }


def _prepare_tree(
    nodes: list[dict[str, Any]],
    project_id: str,
    patches: dict[str, Any],
    deleted_ids: set[str],
    *,
    parent_id: str,
    old_prefix: str = "",
) -> tuple[list[dict[str, Any]], dict[str, str], set[str]]:
    prepared: list[dict[str, Any]] = []
    old_to_new: dict[str, str] = {}
    all_ids: set[str] = set()
    label_occurrences: dict[str, int] = {}
    for index, raw_node in enumerate(nodes, start=1):
        if not isinstance(raw_node, dict):
            continue
        node = deepcopy(raw_node)
        label = str(node.get("label") or "").strip()
        if not label:
            continue
        occurrence_key = label.casefold()
        label_occurrences[occurrence_key] = label_occurrences.get(occurrence_key, 0) + 1
        node_id = str(node.get("id") or "").strip() or _stable_tree_id(
            project_id,
            parent_id,
            label,
            label_occurrences[occurrence_key],
        )
        node["id"] = node_id
        all_ids.add(node_id)
        old_position = f"{old_prefix}.{index}" if old_prefix else str(index)
        old_to_new[f"domain:{old_position}"] = f"domain:{node_id}"
        patch = patches.get(node_id)
        if isinstance(patch, dict) and str(patch.get("label") or "").strip():
            node["label"] = str(patch["label"]).strip()
        raw_children = node.get("child") if isinstance(node.get("child"), list) else []
        children, child_mapping, child_ids = _prepare_tree(
            raw_children,
            project_id,
            patches,
            deleted_ids,
            parent_id=node_id,
            old_prefix=old_position,
        )
        old_to_new.update(child_mapping)
        all_ids.update(child_ids)
        if children:
            node["child"] = children
        else:
            node.pop("child", None)
        if node_id not in deleted_ids:
            prepared.append(node)
    return prepared, old_to_new, all_ids


def _as_dict_list(value: Any) -> list[dict[str, Any]]:
    return [deepcopy(item) for item in value or [] if isinstance(item, dict)]


def apply_project_curation(
    output_dir: Path,
    raw_result: dict[str, Any],
    project_id: str,
    *,
    curation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """把可恢复的人工补丁投影到模型生成结果，并维护图结构一致性。"""
    result = deepcopy(raw_result)
    store = DomainTreeStore()
    normalized = _normalize_curation(curation if curation is not None else store.load_curation(output_dir))
    tree_patches = normalized["treeNodePatches"]
    deleted_tree_ids = {str(value) for value in normalized["deletedTreeNodeIds"]}
    tree, domain_id_mapping, all_tree_ids = _prepare_tree(
        _as_dict_list(result.get("domainTree")),
        project_id,
        tree_patches,
        deleted_tree_ids,
        parent_id=f"project:{project_id}",
    )
    result["domainTree"] = tree

    graph = deepcopy(result.get("knowledgeGraph")) if isinstance(result.get("knowledgeGraph"), dict) else {}
    if not graph:
        result["knowledgeGraph"] = {}
        result["curation"] = {
            "revision": normalized["revision"],
            "updatedAt": normalized["updatedAt"],
            "hasManualChanges": any(
                normalized[key]
                for key in (
                    "treeNodePatches", "deletedTreeNodeIds", "entityPatches", "deletedEntityIds",
                    "relationPatches", "deletedRelationIds",
                )
            ),
            "orphanedPatchCount": sum(key not in all_tree_ids for key in tree_patches),
        }
        return result
    base_entity_ids = {
        str(item.get("id") or "") for item in _as_dict_list(graph.get("entities"))
    }
    base_relation_ids = {
        str(item.get("id") or "") for item in _as_dict_list(graph.get("semanticRelations"))
    }
    has_semantic_layer = "entities" in graph or "semanticRelations" in graph
    entity_patches = normalized["entityPatches"]
    deleted_entity_ids = {str(value) for value in normalized["deletedEntityIds"]}
    entities: list[dict[str, Any]] = []
    for entity in _as_dict_list(graph.get("entities")):
        entity_id = str(entity.get("id") or "")
        if not entity_id or entity_id in deleted_entity_ids:
            continue
        patch = entity_patches.get(entity_id)
        if isinstance(patch, dict):
            entity.update(deepcopy(patch))
        entities.append(entity)
    entity_ids = {str(item.get("id")) for item in entities}

    relation_patches = normalized["relationPatches"]
    deleted_relation_ids = {str(value) for value in normalized["deletedRelationIds"]}
    relations: list[dict[str, Any]] = []
    for relation in _as_dict_list(graph.get("semanticRelations")):
        relation_id = str(relation.get("id") or "")
        if not relation_id or relation_id in deleted_relation_ids:
            continue
        patch = relation_patches.get(relation_id)
        if isinstance(patch, dict):
            relation.update(deepcopy(patch))
        if str(relation.get("source") or "") not in entity_ids or str(relation.get("target") or "") not in entity_ids:
            continue
        relations.append(relation)

    base_nodes = _as_dict_list(graph.get("nodes"))
    replaceable_node_types = {"domain", "subdomain"}
    if has_semantic_layer:
        replaceable_node_types.add("entity")
    nodes = [node for node in base_nodes if str(node.get("type") or "") not in replaceable_node_types]
    project_node_id = f"project:{project_id}"
    node_ids = {str(node.get("id") or "") for node in nodes}
    if project_node_id not in node_ids:
        nodes.append({"id": project_node_id, "name": project_id, "type": "project"})

    effective_domain_ids: set[str] = set()

    def append_domain_nodes(items: list[dict[str, Any]], parent_graph_id: str | None = None) -> None:
        for item in items:
            tree_id = str(item.get("id") or "")
            graph_id = f"domain:{tree_id}"
            effective_domain_ids.add(graph_id)
            nodes.append({
                "id": graph_id,
                "name": str(item.get("label") or ""),
                "type": "subdomain" if parent_graph_id else "domain",
                "treeNodeId": tree_id,
            })
            append_domain_nodes(_as_dict_list(item.get("child")), graph_id)

    append_domain_nodes(tree)
    for entity in entities:
        nodes.append({
            "id": str(entity.get("id") or ""),
            "name": str(entity.get("name") or entity.get("id") or ""),
            "type": "entity",
            "entityType": str(entity.get("type") or "entity"),
            "aliases": deepcopy(entity.get("aliases") or []),
            "attributes": deepcopy(entity.get("attributes") or []),
            "evidenceIds": deepcopy(entity.get("evidenceIds") or []),
        })

    effective_node_ids = {str(node.get("id") or "") for node in nodes}
    edges: list[dict[str, Any]] = []
    for edge in _as_dict_list(graph.get("edges")):
        if has_semantic_layer and str(edge.get("relation") or "") == "semantic_relation":
            continue
        source = domain_id_mapping.get(str(edge.get("source") or ""), str(edge.get("source") or ""))
        target = domain_id_mapping.get(str(edge.get("target") or ""), str(edge.get("target") or ""))
        if source not in effective_node_ids or target not in effective_node_ids:
            continue
        edge["source"] = source
        edge["target"] = target
        if edge.get("relation") not in {"has_domain", "has_subdomain"}:
            edges.append(edge)

    def append_domain_edges(items: list[dict[str, Any]], parent_graph_id: str) -> None:
        for item in items:
            graph_id = f"domain:{item.get('id')}"
            edges.append({
                "source": parent_graph_id,
                "target": graph_id,
                "relation": "has_subdomain" if parent_graph_id.startswith("domain:") else "has_domain",
            })
            append_domain_edges(_as_dict_list(item.get("child")), graph_id)

    append_domain_edges(tree, project_node_id)
    for relation in relations:
        edges.append({
            "source": str(relation.get("source") or ""),
            "target": str(relation.get("target") or ""),
            "relation": "semantic_relation",
            "semanticRelationId": str(relation.get("id") or ""),
            "predicate": str(relation.get("predicate") or ""),
            "relationType": str(relation.get("relationType") or "general"),
            "confidence": relation.get("confidence", 0.5),
            "evidenceIds": deepcopy(relation.get("evidenceIds") or []),
            "documentIds": deepcopy(relation.get("documentIds") or []),
        })

    graph["entities"] = entities
    graph["semanticRelations"] = relations
    graph["nodes"] = nodes
    graph["edges"] = edges
    extraction = deepcopy(graph.get("extraction")) if isinstance(graph.get("extraction"), dict) else {}
    extraction["entityCount"] = len(entities)
    extraction["semanticRelationCount"] = len(relations)
    graph["extraction"] = extraction
    result["knowledgeGraph"] = graph
    orphan_count = (
        sum(key not in all_tree_ids for key in tree_patches)
        + sum(key not in base_entity_ids for key in entity_patches)
        + sum(key not in base_relation_ids for key in relation_patches)
    )
    result["curation"] = {
        "revision": normalized["revision"],
        "updatedAt": normalized["updatedAt"],
        "hasManualChanges": any(
            normalized[key]
            for key in (
                "treeNodePatches", "deletedTreeNodeIds", "entityPatches", "deletedEntityIds",
                "relationPatches", "deletedRelationIds",
            )
        ),
        "orphanedPatchCount": orphan_count,
    }
    return result


class ProjectKnowledgeService:
    """以项目为边界提交领域树和知识图谱修订。"""

    def __init__(self, output_dir: str | Path, project_id: str, *, store: DomainTreeStore | None = None) -> None:
        self.output_dir = Path(output_dir).resolve()
        self.project_id = str(project_id).strip()
        self.store = store or DomainTreeStore()

    def get_result(self) -> dict[str, Any]:
        result = self.store.load_result(self.output_dir, self.project_id)
        if result is None:
            raise KnowledgeNotFoundError("当前项目尚未生成领域树")
        return result

    def update_tree_node(self, node_id: str, patch: dict[str, Any], revision: int) -> dict[str, Any]:
        label = str(patch.get("label") or "").strip()
        if not label:
            raise KnowledgeCurationError("领域节点名称不能为空")
        return self._mutate("tree", node_id, revision, patch={"label": label})

    def delete_tree_node(self, node_id: str, revision: int, *, dry_run: bool = False) -> dict[str, Any]:
        return self._mutate("tree", node_id, revision, delete=True, dry_run=dry_run)

    def restore_tree_node(self, node_id: str, revision: int) -> dict[str, Any]:
        return self._mutate("tree", node_id, revision, restore=True)

    def update_entity(self, entity_id: str, patch: dict[str, Any], revision: int) -> dict[str, Any]:
        allowed = {key: deepcopy(value) for key, value in patch.items() if key in {"name", "type", "aliases", "attributes"}}
        if "name" in allowed and not str(allowed["name"]).strip():
            raise KnowledgeCurationError("实体名称不能为空")
        if "type" in allowed and not str(allowed["type"]).strip():
            raise KnowledgeCurationError("实体类型不能为空")
        if not allowed:
            raise KnowledgeCurationError("没有可修改的实体字段")
        return self._mutate("entity", entity_id, revision, patch=allowed)

    def delete_entity(self, entity_id: str, revision: int, *, dry_run: bool = False) -> dict[str, Any]:
        return self._mutate("entity", entity_id, revision, delete=True, dry_run=dry_run)

    def restore_entity(self, entity_id: str, revision: int) -> dict[str, Any]:
        return self._mutate("entity", entity_id, revision, restore=True)

    def update_relation(self, relation_id: str, patch: dict[str, Any], revision: int) -> dict[str, Any]:
        allowed = {
            key: deepcopy(value)
            for key, value in patch.items()
            if key in {"source", "target", "predicate", "relationType", "confidence"}
        }
        if "predicate" in allowed and not str(allowed["predicate"]).strip():
            raise KnowledgeCurationError("关系谓词不能为空")
        if not allowed:
            raise KnowledgeCurationError("没有可修改的关系字段")
        return self._mutate("relation", relation_id, revision, patch=allowed)

    def delete_relation(self, relation_id: str, revision: int, *, dry_run: bool = False) -> dict[str, Any]:
        return self._mutate("relation", relation_id, revision, delete=True, dry_run=dry_run)

    def restore_relation(self, relation_id: str, revision: int) -> dict[str, Any]:
        return self._mutate("relation", relation_id, revision, restore=True)

    def _mutate(
        self,
        kind: str,
        target_id: str,
        revision: int,
        *,
        patch: dict[str, Any] | None = None,
        delete: bool = False,
        restore: bool = False,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        target_id = str(target_id).strip()
        with _lock_for(self.output_dir):
            raw = self.store.load_raw_result(self.output_dir, self.project_id)
            if raw is None:
                raise KnowledgeNotFoundError("当前项目尚未生成领域树")
            curation = _normalize_curation(self.store.load_curation(self.output_dir))
            if int(revision) != int(curation["revision"]):
                raise KnowledgeRevisionConflict("修订版本已过期，请刷新后重试")
            effective = apply_project_curation(self.output_dir, raw, self.project_id, curation=curation)
            impact = self._impact(effective, kind, target_id)
            deleted_key = {
                "tree": "deletedTreeNodeIds",
                "entity": "deletedEntityIds",
                "relation": "deletedRelationIds",
            }[kind]
            patch_key = {"tree": "treeNodePatches", "entity": "entityPatches", "relation": "relationPatches"}[kind]
            if not impact["exists"] and not restore:
                raise KnowledgeNotFoundError("要修改的对象不存在或已被删除")
            if restore and not impact["exists"]:
                base_result = apply_project_curation(
                    self.output_dir,
                    raw,
                    self.project_id,
                    curation=_normalize_curation({}),
                )
                if not self._impact(base_result, kind, target_id)["exists"]:
                    raise KnowledgeNotFoundError("要恢复的对象不存在于生成结果中")
            if dry_run:
                return {"status": "preview", "impact": impact, "revision": curation["revision"]}
            deleted_ids = {str(value) for value in curation[deleted_key]}
            if delete:
                deleted_ids.add(target_id)
            elif restore:
                deleted_ids.discard(target_id)
            elif patch is not None:
                if kind == "relation":
                    graph = effective.get("knowledgeGraph") if isinstance(effective.get("knowledgeGraph"), dict) else {}
                    entity_ids = {str(item.get("id") or "") for item in _as_dict_list(graph.get("entities"))}
                    relation = next(
                        (item for item in _as_dict_list(graph.get("semanticRelations")) if str(item.get("id") or "") == target_id),
                        {},
                    )
                    source = str(patch.get("source", relation.get("source")) or "")
                    target = str(patch.get("target", relation.get("target")) or "")
                    if source not in entity_ids or target not in entity_ids:
                        raise KnowledgeCurationError("关系起点和终点必须是当前图谱中的实体")
                current_patch = curation[patch_key].get(target_id)
                merged_patch = dict(current_patch) if isinstance(current_patch, dict) else {}
                merged_patch.update(deepcopy(patch))
                curation[patch_key][target_id] = merged_patch
            curation[deleted_key] = sorted(deleted_ids)
            curation["revision"] += 1
            curation["updatedAt"] = datetime.now(timezone.utc).isoformat()
            self.store.save_curation(self.output_dir, curation)
            result = apply_project_curation(self.output_dir, raw, self.project_id, curation=curation)
            return {"status": "ok", "impact": impact, **result}

    @staticmethod
    def _impact(result: dict[str, Any], kind: str, target_id: str) -> dict[str, Any]:
        if kind == "tree":
            def find(nodes: list[dict[str, Any]]) -> dict[str, Any] | None:
                for node in nodes:
                    if str(node.get("id") or "") == target_id:
                        return node
                    found = find(_as_dict_list(node.get("child")))
                    if found:
                        return found
                return None

            node = find(_as_dict_list(result.get("domainTree")))
            descendants = 0
            if node:
                stack = _as_dict_list(node.get("child"))
                while stack:
                    child = stack.pop()
                    descendants += 1
                    stack.extend(_as_dict_list(child.get("child")))
            return {"exists": node is not None, "descendantCount": descendants}
        graph = result.get("knowledgeGraph") if isinstance(result.get("knowledgeGraph"), dict) else {}
        if kind == "entity":
            entities = _as_dict_list(graph.get("entities"))
            relations = _as_dict_list(graph.get("semanticRelations"))
            exists = any(str(item.get("id") or "") == target_id for item in entities)
            relation_count = sum(
                str(item.get("source") or "") == target_id or str(item.get("target") or "") == target_id
                for item in relations
            )
            return {"exists": exists, "relationCount": relation_count}
        relations = _as_dict_list(graph.get("semanticRelations"))
        return {"exists": any(str(item.get("id") or "") == target_id for item in relations)}


__all__ = [
    "KnowledgeCurationError", "KnowledgeNotFoundError", "KnowledgeRevisionConflict",
    "ProjectKnowledgeService", "apply_project_curation",
]
