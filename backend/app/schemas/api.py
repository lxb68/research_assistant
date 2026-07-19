"""HTTP API request models shared by the feature routers."""

from typing import Literal

from pydantic import BaseModel, Field, model_validator


class DatasetDownloadRequest(BaseModel):
    keyword: str = Field(..., min_length=1)
    sources: list[str] = Field(default_factory=lambda: ["arxiv", "crossref"])
    limit_per_source: int = Field(10, ge=1, le=200)
    download_pdf: bool = True
    year_from: int | None = Field(None, ge=1900, le=2100)
    year_to: int | None = Field(None, ge=1900, le=2100)
    min_impact_factor: float | None = Field(None, ge=0)
    ccf_levels: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_year_range(self) -> "DatasetDownloadRequest":
        if self.year_from is not None and self.year_to is not None and self.year_from > self.year_to:
            raise ValueError("year_from cannot be greater than year_to")
        return self


class ManualPdfLinkRequest(BaseModel):
    pdf_path: str = Field(..., min_length=1)
    record_id: str | None = None
    doi: str | None = None
    title: str | None = None


class DeletePapersRequest(BaseModel):
    ids: list[str] = Field(..., min_length=1, max_length=500)


class CleanupMissingPdfsRequest(BaseModel):
    ids: list[str] = Field(..., min_length=1, max_length=5000)


class DeduplicatePapersRequest(BaseModel):
    record_id: str | None = None


class ImportPaperRequest(BaseModel):
    raw_text: str = ""
    title: str = ""
    authors: list[str] = Field(default_factory=list)
    abstract: str = ""
    year: str = ""
    doi: str = ""
    url: str = ""
    pdf_url: str = ""
    custom_tags: list[str] = Field(default_factory=list)


class DomainTreeGenerateOptions(BaseModel):
    action: Literal["rebuild", "revise", "keep"] = "rebuild"
    language: Literal["auto", "中文", "English"] = "auto"
    all_toc: str | None = None
    new_toc: str | None = None
    delete_toc: str | None = None
    model: str | None = None


class DomainTreeGenerateRequest(DomainTreeGenerateOptions):
    project_id: str = Field(..., min_length=1)


class ProjectCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    description: str = Field("", max_length=2000)
    paper_ids: list[str] = Field(default_factory=list, max_length=500)


class ProjectPapersRequest(BaseModel):
    paper_ids: list[str] = Field(default_factory=list, max_length=500)


class ModelConfigRequest(BaseModel):
    provider: str = ""
    protocol: str = ""
    model: str = Field(..., min_length=1)
    base_url: str = Field(..., min_length=1)
    api_key: str = ""
    allow_heuristic_fallback: bool = False


class ModelDiscoveryRequest(BaseModel):
    provider: str = ""
    protocol: str = ""
    base_url: str = Field(..., min_length=1)
    api_key: str = ""


class ModelConnectionTestRequest(ModelConfigRequest):
    pass


class EnvConfigUpdateRequest(BaseModel):
    values: dict[str, str | int | float | bool | None] = Field(default_factory=dict, max_length=100)


class ChatSource(BaseModel):
    index: int = Field(..., ge=1, le=1000)
    record_id: str = Field("", max_length=200)
    title: str = Field("", max_length=2000)
    year: str = Field("", max_length=20)
    section: str = Field("", max_length=2000)
    chunk_index: int = Field(0, ge=0)
    excerpt: str = Field("", max_length=4000)


class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(..., min_length=1, max_length=20000)
    sources: list[ChatSource] = Field(default_factory=list, max_length=20)


class ResearchChatRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=20000)
    history: list[ChatMessage] = Field(default_factory=list, max_length=20)
    paper_ids: list[str] = Field(default_factory=list, max_length=100)
    project_id: str = Field("workspace-domain-tree", min_length=1, max_length=200)


class OrchestratorRequest(BaseModel):
    task: str = Field(..., min_length=1, max_length=20000)
    action: Literal["auto", "direct", "chat", "search", "domain_tree"] = "auto"
    arguments: dict = Field(default_factory=dict)


__all__ = [
    "CleanupMissingPdfsRequest", "DatasetDownloadRequest", "DeduplicatePapersRequest", "DeletePapersRequest",
    "DomainTreeGenerateOptions", "DomainTreeGenerateRequest", "ImportPaperRequest", "ManualPdfLinkRequest",
    "EnvConfigUpdateRequest", "ModelConfigRequest", "ModelConnectionTestRequest", "ModelDiscoveryRequest", "OrchestratorRequest",
    "ProjectCreateRequest", "ProjectPapersRequest",
    "ResearchChatRequest",
]
