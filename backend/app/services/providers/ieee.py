from app.core.config import settings
from app.schemas.paper import paper_item
from app.utils.http import get_json
from app.utils.text import clean_text


IEEE_API_URL = "https://ieeexploreapi.ieee.org/api/v1/search/articles"


def search_ieee(query: str, limit: int = 10) -> list[dict]:
    """调用 IEEE Xplore API 搜索 IEEE 文献元数据。"""
    if not settings.ieee_api_key:
        raise ValueError("IEEE 搜索需要先在 backend/.env 中配置 IEEE_API_KEY")

    data = get_json(
        IEEE_API_URL,
        {
            "apikey": settings.ieee_api_key,
            "querytext": query,
            "max_records": limit,
            "start_record": 1,
            "sort_field": "publication_year",
            "sort_order": "desc",
        },
    )

    papers = []

    for item in data.get("articles", []):
        authors = []
        for author in item.get("authors", {}).get("authors", []):
            name = clean_text(author.get("full_name", ""))
            if name:
                authors.append(name)

        papers.append(
            paper_item(
                source="ieee",
                title=clean_text(item.get("title", "")),
                authors=authors,
                abstract=clean_text(item.get("abstract", "")),
                year=str(item.get("publication_year", "")),
                published_at=clean_text(item.get("publication_date", "")),
                venue=clean_text(item.get("publication_title", "")),
                doi=item.get("doi", ""),
                url=item.get("html_url", "") or item.get("pdf_url", ""),
                pdf_url=item.get("pdf_url", ""),
                external_id=str(item.get("article_number", "")),
            )
        )

    return papers
