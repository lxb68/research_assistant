from app.services.ccf_catalog import CcfCatalog
from app.services.sjr_metrics import SjrMetrics


def enrich_paper_metrics(paper: dict, *, ccf_catalog: CcfCatalog, sjr_metrics: SjrMetrics) -> dict:
    """补充 SJR/影响因子代理指标和 CCF 信息。"""
    if str(paper.get("source", "")).lower() == "arxiv":
        return {
            **paper,
            "impactFactor": None,
            "sjr": None,
            "metricSource": "",
            "ccfLevel": "",
            "ccfSource": "",
            "ccfMatchedName": "",
        }

    venue_text = _metric_text(paper)
    ccf_data = ccf_catalog.lookup(venue_text)
    sjr_data = sjr_metrics.lookup(str(paper.get("venue", "")))

    return {
        **paper,
        "impactFactor": sjr_data.get("impactFactor"),
        "sjr": sjr_data.get("sjr"),
        "metricSource": sjr_data.get("metricSource", ""),
        "ccfLevel": ccf_data.get("ccfLevel", ""),
        "ccfSource": ccf_data.get("ccfSource", ""),
        "ccfMatchedName": ccf_data.get("ccfMatchedName", ""),
    }


def _metric_text(paper: dict) -> str:
    values = [
        str(paper.get("venue", "")),
        str(paper.get("journal", "")),
        str(paper.get("containerTitle", "")),
        str(paper.get("title", "")),
    ]
    return " ".join(values).lower()
