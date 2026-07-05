from app.schemas.paper import paper_item
from app.utils.http import get_json
from app.utils.text import clean_text


OPENALEX_WORKS_API_URL = "https://api.openalex.org/works"


def search_open_access(query: str, limit: int = 10) -> list[dict]:
    """搜索合法开放获取论文，优先返回 OpenAlex 提供的开放 PDF 链接。"""
    data = get_json(
        OPENALEX_WORKS_API_URL,
        {
            "search": query,
            "filter": "is_oa:true",
            "per-page": limit,
        },
        headers={
            "User-Agent": "research-assistant/0.1 (mailto:example@example.com)",
        },
    )
    papers = []

    for item in data.get("results", []):
        primary_location = item.get("primary_location") or {}
        best_oa_location = item.get("best_oa_location") or {}
        open_access = item.get("open_access") or {}
        source = primary_location.get("source") or best_oa_location.get("source") or {}

        pdf_url = primary_location.get("pdf_url") or best_oa_location.get("pdf_url") or ""
        landing_url = (
            primary_location.get("landing_page_url")
            or best_oa_location.get("landing_page_url")
            or open_access.get("oa_url")
            or item.get("doi")
            or item.get("id")
            or ""
        )

        papers.append(
            paper_item(
                source="open_access",
                title=clean_text(item.get("title", "")),
                authors=_extract_authors(item),
                abstract=clean_text(_rebuild_abstract(item.get("abstract_inverted_index") or {})),
                year=str(item.get("publication_year") or ""),
                venue=clean_text(source.get("display_name", "")),
                doi=_normalize_doi(item.get("doi", "")),
                url=landing_url,
                pdf_url=pdf_url,
                external_id=item.get("id", ""),
            )
        )

    return papers


def _extract_authors(item: dict) -> list[str]:
    authors = []
    for authorship in item.get("authorships", []):
        author = authorship.get("author") or {}
        name = clean_text(author.get("display_name", ""))
        if name:
            authors.append(name)
    return authors


def _rebuild_abstract(inverted_index: dict) -> str:
    if not inverted_index:
        return ""

    words: list[tuple[int, str]] = []
    for word, positions in inverted_index.items():
        for position in positions:
            words.append((position, word))

    return " ".join(word for _, word in sorted(words))


def _normalize_doi(value: str) -> str:
    return value.replace("https://doi.org/", "").strip()
