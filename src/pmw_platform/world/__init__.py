"""Durable world adapters and research records."""

from .legacy_frontier import (
    LegacyFrontierView,
    LegacyProblemView,
    build_legacy_frontier_view,
)
from .records import (
    MAXIMUM_RECORD_BYTES,
    RESEARCH_KINDS,
    RESEARCH_RECORD_SCHEMA,
    ResearchContribution,
    ResearchRecord,
    ResearchRecordError,
)
from .store import (
    BoundResearchSession,
    DEFAULT_WORLD_REF,
    PmwWriterAuthority,
    PublishResult,
    ResearchWorld,
    ResearchWorldError,
    WorldAdmission,
)
from .situation import (
    MathematicalSituation,
    SITUATION_SCHEMA,
    build_mathematical_situation,
)

__all__ = [
    "DEFAULT_WORLD_REF",
    "BoundResearchSession",
    "LegacyFrontierView",
    "LegacyProblemView",
    "MAXIMUM_RECORD_BYTES",
    "MathematicalSituation",
    "PmwWriterAuthority",
    "PublishResult",
    "RESEARCH_KINDS",
    "RESEARCH_RECORD_SCHEMA",
    "ResearchContribution",
    "ResearchRecord",
    "ResearchRecordError",
    "ResearchWorld",
    "ResearchWorldError",
    "WorldAdmission",
    "build_legacy_frontier_view",
    "build_mathematical_situation",
    "SITUATION_SCHEMA",
]
