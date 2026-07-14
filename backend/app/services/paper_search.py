"""统一调度各论文数据源，并规范检索结果结构。"""

from app.services.providers.arxiv import search_arxiv
from app.services.providers.crossref import search_crossref
from app.services.providers.ieee import search_ieee
from app.services.providers.open_access import search_open_access
from app.services.providers.pubmed import search_pubmed


SUPPORTED_SOURCES = {
    "arxiv": search_arxiv,
    "pubmed": search_pubmed,
    "crossref": search_crossref,
    "ieee": search_ieee,
    "open_access": search_open_access,
}


def search_papers(source: str, query: str, limit: int = 10) -> dict:
    """根据 source 分发到不同文献平台，并返回统一结构。"""
    source_key = source.lower().strip()
    keyword = query.strip()

    if not keyword:
        raise ValueError("搜索关键词不能为空")

    if source_key not in SUPPORTED_SOURCES:
        supported = ", ".join(SUPPORTED_SOURCES)
        raise ValueError(f"暂不支持该来源：{source}。当前支持：{supported}")

    safe_limit = max(1, min(limit, 200))
    results = SUPPORTED_SOURCES[source_key](keyword, safe_limit)

    return {
        "query": keyword,
        "source": source_key,
        "count": len(results),
        "results": results,
    }
