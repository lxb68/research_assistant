"""计算语料、入选证据与显式目标范围的全文可用性。"""

from __future__ import annotations

from typing import Any, Callable


class EvidenceAvailabilityEvaluator:
    """只评估文档能力，不判断证据是否语义支持用户结论。"""

    def evaluate(
        self,
        papers: list[dict[str, Any]],
        evidence: list[dict[str, Any]],
        *,
        read_full_text: Callable[[dict[str, Any]], str],
    ) -> dict[str, Any]:
        paper_by_id = {
            str(paper.get("id") or paper.get("recordId") or ""): paper
            for paper in papers
            if str(paper.get("id") or paper.get("recordId") or "")
        }
        full_text_ids = {
            paper_id
            for paper_id, paper in paper_by_id.items()
            if bool(read_full_text(paper))
        }
        selected_ids = list(dict.fromkeys(
            str(item.get("record_id") or item.get("recordId") or "")
            for item in evidence
            if str(item.get("record_id") or item.get("recordId") or "")
        ))
        selected_known_ids = [paper_id for paper_id in selected_ids if paper_id in paper_by_id]
        selected_full_text_ids = [paper_id for paper_id in selected_known_ids if paper_id in full_text_ids]
        selected_without_full_text_ids = [
            paper_id
            for paper_id in selected_known_ids
            if paper_id not in full_text_ids
        ]
        relevant_full_text_available = bool(selected_full_text_ids)

        return {
            "paperCount": len(papers),
            "fullTextPaperCount": len(full_text_ids),
            "corpusFullTextComplete": bool(papers) and len(full_text_ids) == len(papers),
            "fullTextPaperIds": sorted(full_text_ids),
            "selectedEvidencePaperCount": len(selected_ids),
            "selectedEvidenceFullTextPaperCount": len(selected_full_text_ids),
            "selectedEvidenceFullTextComplete": (
                bool(selected_known_ids)
                and len(selected_full_text_ids) == len(selected_known_ids)
            ),
            "selectedEvidenceWithoutFullTextIds": selected_without_full_text_ids,
            "relevantFullTextAvailable": relevant_full_text_available,
            # 兼容现有回答策略；语义改为“相关入选证据存在全文”，不再表示整个语料完整。
            "fullTextAvailable": relevant_full_text_available,
        }


__all__ = ["EvidenceAvailabilityEvaluator"]
