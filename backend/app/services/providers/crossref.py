"""通过 Crossref REST API 检索 DOI 与出版物元数据。"""

from app.schemas.paper import paper_item
from app.utils.http import get_json
from app.utils.text import clean_text


CROSSREF_API_URL = "https://api.crossref.org/works"


def search_crossref(query: str, limit: int = 10) -> list[dict]:
    """调用 Crossref REST API 搜索 DOI 和出版物元数据。"""
    data = get_json(
        CROSSREF_API_URL,
        {
            "query": query,
            "rows": limit,
        },
        headers={
            "User-Agent": "research-assistant/0.1 (mailto:example@example.com)",
        },
    )
    # 解析返回的 JSON 数据
    items = data.get("message", {}).get("items", [])
    papers = []

    for item in items:
        # 处理每个论文
        # 处理作者列表
        authors = []
        for author in item.get("author", []):
            name = " ".join(
                part
                for part in [author.get("given", ""), author.get("family", "")]
                if part
            )
            if name:
                authors.append(name)
        # 处理年份
        published = item.get("published-print") or item.get("published-online") or item.get("created") or {}
        date_parts = published.get("date-parts", [[]])
        year = str(date_parts[0][0]) if date_parts and date_parts[0] else ""

        papers.append(
            paper_item(
                source="crossref",
                title=clean_text((item.get("title") or [""])[0]),
                authors=authors,
                abstract=clean_text(item.get("abstract", "")),
                year=year,
                venue=clean_text((item.get("container-title") or [""])[0]),
                doi=item.get("DOI", ""),
                url=item.get("URL", ""),
                external_id=item.get("DOI", ""),
            )
        )

    return papers
