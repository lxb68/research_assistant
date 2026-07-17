"""按语义结构组织检索证据，避免算法、表格等连续分块被下游再次拆散。"""

from __future__ import annotations

from collections import OrderedDict
from typing import Any, Iterable


EvidenceGroupKey = tuple[str, str, str | int]


def evidence_group_key(item: dict[str, Any]) -> EvidenceGroupKey:
    """返回稳定的逻辑证据键；无结构片段退化为单块证据。"""
    record_id = str(item.get("record_id") or "")
    structure_id = str(item.get("structure_id") or "").strip()
    if structure_id:
        return (record_id, "structure", structure_id)
    return (record_id, "chunk", int(item.get("chunk_index") or 0))


def group_evidence(items: Iterable[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """按首次出现顺序分组，并把结构成员恢复为原始顺序。"""
    grouped: OrderedDict[EvidenceGroupKey, list[dict[str, Any]]] = OrderedDict()
    for item in items:
        grouped.setdefault(evidence_group_key(item), []).append(item)
    result = list(grouped.values())
    for group in result:
        if str(group[0].get("structure_id") or "").strip():
            group.sort(
                key=lambda item: (
                    int(item.get("structure_sequence") or 0),
                    int(item.get("chunk_index") or 0),
                )
            )
    return result


def flatten_evidence_groups(groups: Iterable[Iterable[dict[str, Any]]]) -> list[dict[str, Any]]:
    """把逻辑证据组展开为兼容现有回答链的片段列表。"""
    return [item for group in groups for item in group]


def limit_evidence_groups(
    items: Iterable[dict[str, Any]],
    *,
    max_groups: int,
) -> list[dict[str, Any]]:
    """按逻辑证据数量裁剪；同一结构的成员必须整体保留。"""
    groups = group_evidence(items)
    return flatten_evidence_groups(groups[: max(1, int(max_groups))])


def evidence_group_is_complete(group: list[dict[str, Any]]) -> bool:
    """判断当前上下文是否覆盖一个连续结构的首尾及所有中间序号。"""
    if not group or not str(group[0].get("structure_id") or "").strip():
        return True
    ordered = sorted(group, key=lambda item: int(item.get("structure_sequence") or 0))
    sequences = [int(item.get("structure_sequence") or 0) for item in ordered]
    contiguous = sequences == list(range(sequences[0], sequences[0] + len(sequences)))
    return (
        contiguous
        and not ordered[0].get("continues_from")
        and not ordered[-1].get("continues_to")
    )


__all__ = [
    "evidence_group_is_complete",
    "evidence_group_key",
    "flatten_evidence_groups",
    "group_evidence",
    "limit_evidence_groups",
]
