"""定义跨文献平台共用的论文响应结构。"""


def paper_item(
    *,
    source: str,
    title: str,
    authors: list[str] | None = None,
    abstract: str = "",
    year: str = "",
    published_at: str = "",
    venue: str = "",
    doi: str = "",
    url: str = "",
    pdf_url: str = "",
    external_id: str = "",
) -> dict:
    """统一不同文献平台的返回字段，方便前端用同一种结构渲染。"""
    return {
        "source": source,
        "title": title,
        "authors": authors or [],
        "abstract": abstract,
        "year": year,
        "publishedAt": published_at,
        "venue": venue,
        "doi": doi,
        "url": url,
        "pdfUrl": pdf_url,
        "externalId": external_id,
    }
