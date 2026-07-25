from __future__ import annotations

from unittest.mock import Mock

from app.services.paper_repository import PaperRepository
from app.services.zotero_collection_tree import (
    LIBRARY_ROOT_KEY,
    UNFILED_KEY,
    ZoteroCollectionRepository,
    ZoteroCollectionTreeService,
)
from app.services.zotero_sync import ZoteroSyncService


COLLECTIONS = [
    {"key": "ROOT", "name": "课题", "parentCollection": "", "version": 1},
    {"key": "METHOD", "name": "方法", "parentCollection": "ROOT", "version": 2},
    {"key": "VLA", "name": "VLA", "parentCollection": "METHOD", "version": 3},
    {"key": "OTHER", "name": "其他", "parentCollection": "", "version": 1},
]


def test_selected_collection_preserves_all_descendant_levels() -> None:
    nodes, collection_keys, whole_library = ZoteroCollectionTreeService().build_scope(
        COLLECTIONS,
        root_keys=["root"],
        include_subcollections=True,
    )

    by_key = {node["key"]: node for node in nodes}
    assert whole_library is False
    assert collection_keys == ["ROOT", "METHOD", "VLA"]
    assert by_key["ROOT"]["parentKey"] == ""
    assert by_key["ROOT"]["depth"] == 0
    assert by_key["METHOD"]["parentKey"] == "ROOT"
    assert by_key["METHOD"]["depth"] == 1
    assert by_key["VLA"]["parentKey"] == "METHOD"
    assert by_key["VLA"]["depth"] == 2
    assert by_key["VLA"]["path"] == "课题 / 方法 / VLA"


def test_selected_collection_can_exclude_descendants() -> None:
    nodes, collection_keys, _ = ZoteroCollectionTreeService().build_scope(
        COLLECTIONS,
        root_keys=["ROOT"],
        include_subcollections=False,
    )

    assert collection_keys == ["ROOT"]
    assert [node["key"] for node in nodes] == ["ROOT"]


def test_whole_library_has_virtual_root_and_unfiled_node() -> None:
    nodes, collection_keys, whole_library = ZoteroCollectionTreeService().build_scope(
        COLLECTIONS,
        root_keys=[],
        include_subcollections=True,
    )
    by_key = {node["key"]: node for node in nodes}

    assert whole_library is True
    assert collection_keys == ["OTHER", "ROOT", "METHOD", "VLA"]
    assert by_key[LIBRARY_ROOT_KEY]["parentKey"] == ""
    assert by_key["ROOT"]["parentKey"] == LIBRARY_ROOT_KEY
    assert by_key["VLA"]["parentKey"] == "METHOD"
    assert by_key[UNFILED_KEY]["parentKey"] == LIBRARY_ROOT_KEY


def test_snapshot_keeps_multi_collection_membership_without_copying_paper(tmp_path) -> None:
    database = tmp_path / "metadata.sqlite3"
    papers = PaperRepository(database)
    papers.save({"id": "paper-1", "source": "zotero", "title": "共享论文"})
    repository = ZoteroCollectionRepository(database)
    nodes, _, _ = ZoteroCollectionTreeService().build_scope(
        COLLECTIONS,
        root_keys=["ROOT"],
        include_subcollections=True,
    )

    repository.replace_snapshot(
        "source-1",
        nodes=nodes,
        memberships={"ITEM1": {"ROOT", "VLA"}},
        paper_ids={"ITEM1": "paper-1"},
    )

    roots = repository.list_tree("source-1")
    assert len(roots) == 1
    assert roots[0]["key"] == "ROOT"
    assert roots[0]["paperCount"] == 1
    assert roots[0]["directPaperCount"] == 1
    assert roots[0]["children"][0]["children"][0]["paperCount"] == 1
    assert repository.list_paper_ids("source-1", "ROOT") == ["paper-1"]
    assert repository.list_paper_ids(
        "source-1",
        "ROOT",
        include_descendants=False,
    ) == ["paper-1"]


def test_snapshot_updates_move_and_name_in_place(tmp_path) -> None:
    database = tmp_path / "metadata.sqlite3"
    repository = ZoteroCollectionRepository(database)
    service = ZoteroCollectionTreeService()
    initial, _, _ = service.build_scope(
        COLLECTIONS,
        root_keys=["ROOT"],
        include_subcollections=True,
    )
    repository.replace_snapshot("source-1", nodes=initial, memberships={}, paper_ids={})

    moved_values = [
        {"key": "ROOT", "name": "新课题", "parentCollection": "", "version": 4},
        {"key": "VLA", "name": "视觉语言动作", "parentCollection": "ROOT", "version": 5},
    ]
    moved, _, _ = service.build_scope(
        moved_values,
        root_keys=["ROOT"],
        include_subcollections=True,
    )
    repository.replace_snapshot("source-1", nodes=moved, memberships={}, paper_ids={})

    root = repository.list_tree("source-1")[0]
    assert root["name"] == "新课题"
    assert [child["key"] for child in root["children"]] == ["VLA"]
    assert root["children"][0]["path"] == "新课题 / 视觉语言动作"


def test_sync_snapshot_deduplicates_item_and_keeps_every_membership() -> None:
    shared_item = {
        "key": "ITEM1",
        "data": {"key": "ITEM1", "itemType": "journalArticle", "title": "共享论文"},
    }
    connector = Mock()
    connector.list_collections.return_value = COLLECTIONS
    connector.list_top_items.side_effect = lambda key=None: (
        [shared_item] if key in {"ROOT", "VLA"} else []
    )
    service = ZoteroSyncService.__new__(ZoteroSyncService)
    service.collection_tree = ZoteroCollectionTreeService()

    snapshot = service._load_source_snapshot(
        connector,
        {
            "collectionKeys": ["ROOT"],
            "includeSubcollections": True,
            "includeStandaloneAttachments": False,
        },
    )

    assert [item["key"] for item in snapshot.items] == ["ITEM1"]
    assert snapshot.memberships == {"ITEM1": {"ROOT", "VLA"}}
    assert [node["key"] for node in snapshot.nodes] == ["ROOT", "METHOD", "VLA"]
