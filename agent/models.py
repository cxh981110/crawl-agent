from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class FieldRule:
    labels: list[str]
    field_type: str = "string"


@dataclass
class ExtractionEvidence:
    source: str
    locator: Optional[str] = None
    matched_label: Optional[str] = None
    matched_text: Optional[str] = None
    response_url: Optional[str] = None


@dataclass
class ExtractionResult:
    url: str
    title: Optional[str]
    strategy: str
    data: dict[str, Any]
    missing_fields: list[str]
    evidence: dict[str, Optional[dict[str, Any]]] = field(default_factory=dict)
    candidate_count: int = 0
