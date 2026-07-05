from xml.etree import ElementTree

from app.core.config import settings
from app.schemas.paper import paper_item
from app.utils.http import get_json, get_text
from app.utils.text import clean_text


ESEARCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
EFETCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
NCBI_HEADERS = {
    "User-Agent": "research-assistant/0.1 (mailto:example@example.com)",
}


def search_pubmed(query: str, limit: int = 10) -> list[dict]:
    """调用 PubMed E-utilities 搜索生物医学文献。"""
    search_params = {
        "db": "pubmed",
        "term": query,
        "retmode": "json",
        "retmax": limit,
        "tool": "research_assistant",
    }

    if settings.ncbi_email:
        search_params["email"] = settings.ncbi_email
    if settings.ncbi_api_key:
        search_params["api_key"] = settings.ncbi_api_key

    search_data = get_json(ESEARCH_URL, search_params, headers=NCBI_HEADERS)
    ids = search_data.get("esearchresult", {}).get("idlist", [])

    if not ids:
        return []

    fetch_params = {
        "db": "pubmed",
        "id": ",".join(ids),
        "retmode": "xml",
        "tool": "research_assistant",
    }

    if settings.ncbi_email:
        fetch_params["email"] = settings.ncbi_email
    if settings.ncbi_api_key:
        fetch_params["api_key"] = settings.ncbi_api_key

    xml_text = get_text(EFETCH_URL, fetch_params, headers=NCBI_HEADERS)
    root = ElementTree.fromstring(xml_text)
    papers = []

    for article in root.findall(".//PubmedArticle"):
        medline = article.find("MedlineCitation")
        pmid = clean_text(medline.findtext("PMID", default="")) if medline is not None else ""
        article_node = medline.find("Article") if medline is not None else None

        if article_node is None:
            continue

        title = clean_text(article_node.findtext("ArticleTitle", default=""))
        abstract_parts = [
            clean_text(part.text)
            for part in article_node.findall("Abstract/AbstractText")
            if part.text
        ]

        authors = []
        for author in article_node.findall("AuthorList/Author"):
            last_name = clean_text(author.findtext("LastName", default=""))
            fore_name = clean_text(author.findtext("ForeName", default=""))
            collective = clean_text(author.findtext("CollectiveName", default=""))
            name = collective or " ".join(part for part in [fore_name, last_name] if part)
            if name:
                authors.append(name)

        journal = clean_text(article_node.findtext("Journal/Title", default=""))
        year = (
            clean_text(article_node.findtext("Journal/JournalIssue/PubDate/Year", default=""))
            or clean_text(article_node.findtext("ArticleDate/Year", default=""))
        )

        papers.append(
            paper_item(
                source="pubmed",
                title=title,
                authors=authors,
                abstract=" ".join(abstract_parts),
                year=year,
                venue=journal,
                url=f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/" if pmid else "",
                external_id=pmid,
            )
        )

    return papers
