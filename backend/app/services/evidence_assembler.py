"""将已排序的检索候选组装为最终证据上下文。"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable


@dataclass(slots=True)
class EvidenceAssemblyResult:
    """证据组装结果以及可观测的预算、多样性诊断。"""

    evidence: list[Any]
    diagnostics: dict[str, Any] = field(default_factory=dict)


class EvidenceAssembler:
    """以逻辑证据组为单位执行多样性约束和上下文预算分配。"""

    def __init__(
        self,
        *,
        max_chunks: int,
        max_context_chars: int,
        tokenize: Callable[[str], list[str]],
        similarity_threshold: float = 0.82,
    ) -> None:
        self.max_chunks = max(1, int(max_chunks))
        self.max_context_chars = max(1, int(max_context_chars))
        self.tokenize = tokenize
        self.similarity_threshold = float(similarity_threshold)

    def assemble(
        self,
        ranked: Iterable[Any],
        candidates: Iterable[Any],
        *,
        max_groups_per_paper: int,
    ) -> EvidenceAssemblyResult:
        """先恢复连续结构，再在结构之间执行多样性筛选。"""
        ranked_items = list(ranked)
        candidate_items = list(candidates)
        groups = self._ranked_groups(ranked_items, candidate_items)
        selected: list[Any] = []
        selected_group_tokens: list[set[str]] = []
        groups_per_paper: defaultdict[str, int] = defaultdict(int)
        context_chars = 0
        selected_group_count = 0
        dropped_by_diversity = 0
        dropped_by_paper_limit = 0
        dropped_by_budget = 0

        for seed, members in groups:
            record_id = str(seed.record_id)
            if groups_per_paper[record_id] >= max(1, int(max_groups_per_paper)):
                dropped_by_paper_limit += 1
                continue

            group_tokens = set(self.tokenize(" ".join(str(item.text) for item in members)))
            if any(
                self._jaccard(group_tokens, existing) > self.similarity_threshold
                for existing in selected_group_tokens
            ):
                dropped_by_diversity += 1
                continue

            prepared = self._fit_group(
                seed,
                members,
                remaining_chunks=self.max_chunks - len(selected),
                remaining_chars=self.max_context_chars - context_chars,
            )
            if not prepared:
                dropped_by_budget += 1
                continue

            selected.extend(prepared)
            selected_group_tokens.append(group_tokens)
            groups_per_paper[record_id] += 1
            selected_group_count += 1
            context_chars += sum(len(str(item.text)) for item in prepared)
            if len(selected) >= self.max_chunks:
                break

        structured_groups = self._group_selected(selected)
        incomplete_structure_count = sum(
            not self._structure_is_complete(members)
            for key, members in structured_groups.items()
            if key[1] == "structure"
        )
        return EvidenceAssemblyResult(
            evidence=selected,
            diagnostics={
                "candidateGroupCount": len(groups),
                "selectedEvidenceGroupCount": selected_group_count,
                "selectedStructureCount": sum(key[1] == "structure" for key in structured_groups),
                "incompleteStructureCount": incomplete_structure_count,
                "droppedByDiversity": dropped_by_diversity,
                "droppedByPaperLimit": dropped_by_paper_limit,
                "droppedByBudget": dropped_by_budget,
            },
        )

    def _ranked_groups(
        self,
        ranked: list[Any],
        candidates: list[Any],
    ) -> list[tuple[Any, list[Any]]]:
        structure_members: defaultdict[tuple[str, str], list[Any]] = defaultdict(list)
        for item in candidates:
            structure_id = str(getattr(item, "structure_id", "") or "")
            if structure_id:
                structure_members[(str(item.record_id), structure_id)].append(item)
        for members in structure_members.values():
            members.sort(key=lambda item: (int(item.structure_sequence), int(item.chunk_index)))

        result: list[tuple[Any, list[Any]]] = []
        seen: set[tuple[str, str, str | int]] = set()
        for seed in ranked:
            key = self._group_key(seed)
            if key in seen:
                continue
            seen.add(key)
            members = (
                structure_members.get((key[0], str(key[2])), [seed])
                if key[1] == "structure"
                else [seed]
            )
            result.append((seed, members))
        return result

    def _fit_group(
        self,
        seed: Any,
        members: list[Any],
        *,
        remaining_chunks: int,
        remaining_chars: int,
    ) -> list[Any]:
        if remaining_chunks <= 0 or remaining_chars <= 0:
            return []
        if len(members) <= remaining_chunks and self._text_size(members) <= remaining_chars:
            return members
        if not str(getattr(seed, "structure_id", "") or ""):
            return []

        seed_position = next(
            (index for index, item in enumerate(members) if item.chunk_index == seed.chunk_index),
            0,
        )
        window_size = min(remaining_chunks, len(members))
        window_start = max(
            0,
            min(seed_position - window_size // 2, len(members) - window_size),
        )
        window = members[window_start : window_start + window_size]
        while window and self._text_size(window) > remaining_chars:
            seed_offset = next(
                (index for index, item in enumerate(window) if item.chunk_index == seed.chunk_index),
                -1,
            )
            if seed_offset < 0 or len(window) == 1:
                return []
            if seed_offset >= len(window) / 2:
                window = window[1:]
            else:
                window = window[:-1]
        return window

    @staticmethod
    def _group_key(item: Any) -> tuple[str, str, str | int]:
        record_id = str(item.record_id)
        structure_id = str(getattr(item, "structure_id", "") or "")
        if structure_id:
            return (record_id, "structure", structure_id)
        return (record_id, "chunk", int(item.chunk_index))

    def _group_selected(self, items: list[Any]) -> dict[tuple[str, str, str | int], list[Any]]:
        grouped: defaultdict[tuple[str, str, str | int], list[Any]] = defaultdict(list)
        for item in items:
            grouped[self._group_key(item)].append(item)
        return dict(grouped)

    @staticmethod
    def _structure_is_complete(members: list[Any]) -> bool:
        ordered = sorted(members, key=lambda item: int(item.structure_sequence))
        sequences = [int(item.structure_sequence) for item in ordered]
        return (
            sequences == list(range(sequences[0], sequences[0] + len(sequences)))
            and not ordered[0].continues_from
            and not ordered[-1].continues_to
        )

    @staticmethod
    def _text_size(items: list[Any]) -> int:
        return sum(len(str(item.text)) for item in items)

    @staticmethod
    def _jaccard(left: set[str], right: set[str]) -> float:
        if not left or not right:
            return 0.0
        return len(left & right) / len(left | right)


__all__ = ["EvidenceAssembler", "EvidenceAssemblyResult"]
