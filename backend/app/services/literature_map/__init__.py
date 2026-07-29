"""版本化、证据可追溯的文献地图子系统。"""

from app.services.literature_map.builder import LiteratureMapBuildResult, LiteratureMapBuilder
from app.services.literature_map.extractor import LiteratureMapExtractor
from app.services.literature_map.models import (
    EvidenceReference,
    LiteratureRelation,
    MapClaim,
    PaperCard,
    PaperCardDraft,
    PaperExtractionResult,
    RelationCandidate,
)
from app.services.literature_map.metadata_quality import PaperMetadataValidator
from app.services.literature_map.normalization import VocabularyNormalizer
from app.services.literature_map.policy import LiteratureMapExtractionPolicy
from app.services.literature_map.repository import LiteratureMapRepository
from app.services.literature_map.service import (
    LiteratureMapProjectService,
    PaperEvidenceAdapter,
)
from app.services.literature_map.resolution import PaperEntityResolver, RelationMerger
from app.services.literature_map.versioning import compute_document_version

__all__ = [
    "EvidenceReference",
    "LiteratureMapBuildResult",
    "LiteratureMapBuilder",
    "LiteratureMapExtractor",
    "LiteratureMapExtractionPolicy",
    "LiteratureMapRepository",
    "LiteratureMapProjectService",
    "LiteratureRelation",
    "MapClaim",
    "PaperCard",
    "PaperCardDraft",
    "PaperEntityResolver",
    "PaperExtractionResult",
    "PaperEvidenceAdapter",
    "PaperMetadataValidator",
    "RelationCandidate",
    "RelationMerger",
    "VocabularyNormalizer",
    "compute_document_version",
]
