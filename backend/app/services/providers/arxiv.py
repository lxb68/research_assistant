from xml.etree import ElementTree

from app.schemas.paper import paper_item
from app.utils.http import get_text
from app.utils.text import clean_text


ARXIV_API_URL = "https://export.arxiv.org/api/query"
ATOM_NS = {"atom": "http://www.w3.org/2005/Atom"}


def search_arxiv(query: str, limit: int = 10) -> list[dict]:
    """调用 arXiv Atom API 搜索预印本文献。"""
    xml_text = get_text(
        ARXIV_API_URL,
        {
            "search_query": f"all:{query}",
            "start": 0,
            "max_results": limit,
            "sortBy": "submittedDate",
            "sortOrder": "descending",
        },
        headers={
            "User-Agent": "research-assistant/0.1 (mailto:example@example.com)",
        },
    )
    # 将 XML 字符串转为 ElementTree 对象
    root = ElementTree.fromstring(xml_text)
    papers = []

    for entry in root.findall("atom:entry", ATOM_NS):
        # 处理每个论文
        links = entry.findall("atom:link", ATOM_NS)
        page_url = ""
        pdf_url = ""

        for link in links:
            href = link.attrib.get("href", "")
            title = link.attrib.get("title", "")

            if link.attrib.get("rel") == "alternate":
                page_url = href
            if title == "pdf" or href.endswith(".pdf"):
                pdf_url = href
        # 处理作者列表
        authors = [
            clean_text(author.findtext("atom:name", default="", namespaces=ATOM_NS))
            for author in entry.findall("atom:author", ATOM_NS)
        ]
        # 处理发布时间
        published_at = clean_text(entry.findtext("atom:published", default="", namespaces=ATOM_NS))
        # 组装结果
        papers.append(
            paper_item(
                source="arxiv",
                title=clean_text(entry.findtext("atom:title", default="", namespaces=ATOM_NS)),
                authors=[author for author in authors if author],
                abstract=clean_text(entry.findtext("atom:summary", default="", namespaces=ATOM_NS)),
                year=published_at[:4],
                published_at=published_at,
                url=page_url,
                pdf_url=pdf_url,
                external_id=clean_text(entry.findtext("atom:id", default="", namespaces=ATOM_NS)),
            )
        )

    return papers

def search_arxiv_safe(query: str, limit: int = 10) -> list[dict]:
    """带错误处理的搜索函数"""
    try:
        return search_arxiv(query, limit)
    except requests.RequestException as e:
        print(f"网络请求失败: {e}")
        return []
    except ElementTree.ParseError as e:
        print(f"XML 解析失败: {e}")
        return []
    except Exception as e:
        print(f"未知错误: {e}")
        return []