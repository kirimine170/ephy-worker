"""Worker-owned provenance and strict model-stage contracts."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProseModel(StrictModel):
    @field_validator("*", mode="after")
    @classmethod
    def japanese_punctuation(cls, value, info):
        prose_fields = {"text", "reason", "conditions", "uncertainty", "gaps", "subquestions"}
        if info.field_name not in prose_fields:
            return value

        def normalize(item):
            return item.replace("、", "，").replace("。", "．") if isinstance(item, str) else item

        return [normalize(item) for item in value] if isinstance(value, list) else normalize(value)


class Limits(StrictModel):
    max_queries: int = Field(10, ge=1, le=30)
    search_requests: int = Field(20, ge=1, le=100)
    max_sources: int = Field(12, ge=1, le=30)
    fetch_requests: int = Field(36, ge=1, le=100)
    model_requests: int = Field(40, ge=1, le=100)
    job_seconds: float = Field(900, gt=0, le=7200)
    request_seconds: float = Field(120, gt=0, le=600)
    parser_seconds: float = Field(20, gt=0, le=120)
    parser_memory_mb: int = Field(512, ge=128, le=2048)
    html_bytes: int = Field(5 * 1024**2, ge=1024, le=20 * 1024**2)
    pdf_bytes: int = Field(20 * 1024**2, ge=1024, le=50 * 1024**2)
    text_chars: int = Field(200_000, ge=1000, le=1_000_000)
    pdf_pages: int = Field(100, ge=1, le=500)
    cache_bytes: int = Field(128 * 1024**2, ge=1024, le=512 * 1024**2)
    initial_sources: int = Field(5, ge=4, le=6)
    passage_chars: int = Field(1600, ge=200, le=4000)
    selected_passages: int = Field(24, ge=4, le=80)
    max_redirects: int = Field(5, ge=0, le=10)


class Query(ProseModel):
    text: str = Field(min_length=1, max_length=240)
    role: Literal["overview", "primary", "limitations", "gap"]
    reason: str = Field(min_length=1, max_length=1000)


class Plan(ProseModel):
    subquestions: list[str] = Field(min_length=1, max_length=8)
    queries: list[Query] = Field(min_length=3, max_length=5)


class Candidate(StrictModel):
    url: str
    title: str = ""
    snippet: str = ""
    query_id: str = ""
    engine: str = ""
    rank: int = 0
    discovered_at: str = Field(default_factory=utc_now)
    supplied: bool = False


class Passage(StrictModel):
    passage_id: str
    source_id: str
    text: str
    page: int | None = None  # Physical PDF page，one based．
    start: int = 0  # Offset within extracted page text，or HTML body．
    end: int = 0


class Source(StrictModel):
    source_id: str
    requested_url: str
    final_url: str | None = None
    title: str = ""
    query_ids: list[str] = Field(default_factory=list)
    engines: list[str] = Field(default_factory=list)
    supplied: bool = False
    fetched_at: str = Field(default_factory=utc_now)
    http_status: int | None = None
    media_type: str | None = None
    kind: Literal["html", "pdf", "unknown"] = "unknown"
    status: Literal[
        "ok",
        "partial",
        "blocked",
        "http_error",
        "network_error",
        "unsupported",
        "extraction_failed",
        "image_only",
        "encrypted",
        "invalid",
        "timeout",
        "limit",
    ] = "extraction_failed"
    error: str | None = None
    body_hash: str | None = None
    extractor: str | None = None
    bytes_received: int = 0
    publisher: str | None = None
    author: str | None = None
    published_at: str | None = None
    metadata_evidence: dict[str, str] = Field(default_factory=dict)
    origin_urls: list[str] = Field(default_factory=list)
    passages: list[Passage] = Field(default_factory=list)
    page_count: int | None = None
    page_statuses: dict[str, str] = Field(default_factory=dict)
    read_passage_ids: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


class OriginGroup(StrictModel):
    group_id: str
    source_ids: list[str]
    origin_urls: list[str] = Field(default_factory=list)
    reasons: list[str]
    independence: Literal["confirmed", "unknown"] = "unknown"


class EvidenceProposal(ProseModel):
    source_id: str
    passage_id: str
    quote: str = Field(min_length=1, max_length=500)
    relation: Literal["supports", "contradicts", "context_only"]
    conditions: str = ""


ClaimStatus = Literal[
    "supported_primary", "corroborated", "conflicting", "insufficient", "refuted", "inference"
]


class ClaimProposal(ProseModel):
    text: str = Field(min_length=1, max_length=2000)
    importance: Literal["major", "minor"] = "major"
    evidence: list[EvidenceProposal] = Field(default_factory=list, max_length=8)
    conditions: str = ""
    uncertainty: list[str] = Field(default_factory=list)


class Extraction(ProseModel):
    claims: list[ClaimProposal] = Field(default_factory=list, max_length=10)
    gaps: list[str] = Field(default_factory=list, max_length=8)


class Evidence(EvidenceProposal):
    evidence_id: str
    page: int | None = None
    quote_start: int
    quote_end: int
    quote_match: Literal["exact", "whitespace_normalized"] = "exact"
    checked: bool = False
    check_reason: str | None = None
    conditions_match: bool | None = None
    is_primary: bool | None = None


class Claim(ProseModel):
    claim_id: str
    text: str
    importance: Literal["major", "minor"] = "major"
    evidence_ids: list[str] = Field(default_factory=list)
    status: ClaimStatus = "insufficient"
    reason: str = "照合未完了．"
    conditions: str = ""
    uncertainty: list[str] = Field(default_factory=list)
    inference_from: list[str] = Field(default_factory=list)
    checked: bool = False


class EvidenceCheck(ProseModel):
    evidence_id: str
    relation: Literal["supports", "contradicts", "context_only"]
    conditions_match: bool
    is_primary: bool
    reason: str = Field(min_length=1, max_length=1500)


class ClaimCheck(ProseModel):
    claim_id: str
    status: ClaimStatus
    reason: str = Field(min_length=1, max_length=2000)
    conditions: str = ""
    uncertainty: list[str] = Field(default_factory=list, max_length=8)
    evidence_checks: list[EvidenceCheck] = Field(default_factory=list, max_length=8)
    inference_from: list[str] = Field(default_factory=list, max_length=8)


class Review(ProseModel):
    checks: list[ClaimCheck] = Field(max_length=10)
    additional_queries: list[Query] = Field(default_factory=list, max_length=3)
    gaps: list[str] = Field(default_factory=list, max_length=8)


class Selection(StrictModel):
    urls: list[str] = Field(min_length=1, max_length=6)
    reason: str = Field(max_length=1500)


class Report(StrictModel):
    schema_version: Literal["0.1"] = "0.1"
    workflow_version: Literal["phase1-v1"] = "phase1-v1"
    job_id: str
    question: str
    profile_id: str
    created_at: str = Field(default_factory=utc_now)
    finished_at: str | None = None
    state: Literal["running", "completed", "partial", "failed", "cancelled"] = "running"
    answerability: Literal["sufficient", "limited", "unresolved"] = "unresolved"
    stop_reason: str = "running"
    subquestions: list[str] = Field(default_factory=list)
    queries: list[dict] = Field(default_factory=list)
    candidates: list[Candidate] = Field(default_factory=list)
    sources: list[Source] = Field(default_factory=list)
    origin_groups: list[OriginGroup] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    claims: list[Claim] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)
    failures: list[dict] = Field(default_factory=list)
    rounds: list[dict] = Field(default_factory=list)
    metrics: dict = Field(default_factory=dict)
    activity_summary: str = ""
