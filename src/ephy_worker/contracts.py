"""manager ↔ worker HTTP 契約（0.4）：web.collect の job 定義・結果パック．

0.3 からの変更（docs/adr/0003）：

- job kind は ``web.collect``，mode は ``search`` / ``extract`` のみ（discriminated union）．
- ``target_worker_id`` は必須（broadcast claim は廃止）．
- job state は queued / running / cancel_requested / completed / partial /
  failed / cancelled / lost．queued への cancel は即 cancelled（再 queue なし）．
- waiting 理由を構造化（target_worker_unregistered / _offline /
  _capability_mismatch / _contract_mismatch）．
- worker 登録に capabilities / implementation_version / contract_version を必須化．
- 結果パックは typed payload（queries/candidates/sources）+ typed failures +
  typed usage．list[dict] や裸の str で境界を渡さない．
- artifact は job + attempt に紐づけ，名前は安全文字列のみ，media type は許可リスト，
  download は job scope のみ．
- retry は parent の残 budget / 残時間から child budget を導出（全量再付与しない）．
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .schema import Candidate, Limits, Query, Source

CONTRACT_VERSION = "0.4"
JOB_KIND = "web.collect"
COLLECT_MODES: tuple[str, ...] = ("search", "extract")
COLLECT_CAPABILITY = "web.collect"
RESULT_PACK_NAME = "result-pack.json"
RESULT_PACK_MEDIA_TYPE = "application/json"
RESULT_PACK_SCHEMA_VERSION = 1
TERMINAL_STATES: tuple[str, ...] = ("completed", "partial", "failed", "cancelled", "lost")
JOB_STATES: tuple[str, ...] = ("queued", "running", "cancel_requested", *TERMINAL_STATES)
WAIT_REASONS: tuple[str, ...] = (
    "target_worker_unregistered",
    "target_worker_offline",
    "target_worker_capability_mismatch",
    "target_worker_contract_mismatch",
)
MAX_ARTIFACTS_PER_JOB = 128
MAX_JOB_INPUT_CHARS = 20_000


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        default=str,
    )


def sha256_hex(value: str | bytes) -> str:
    data = value.encode("utf-8") if isinstance(value, str) else value
    return hashlib.sha256(data).hexdigest()


def job_input_hash(input: dict) -> str:
    return sha256_hex(canonical_json(input))


def result_pack_hash(payload: dict | BaseModel) -> str:
    value = payload.model_dump(mode="json") if isinstance(payload, BaseModel) else payload
    return sha256_hex(canonical_json(value))


def required_capabilities(mode: str) -> tuple[str, ...]:
    if mode not in COLLECT_MODES:
        raise ValueError(f"unknown collect mode: {mode}")
    return (COLLECT_CAPABILITY,)


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class JobQuery(ContractModel):
    """research 側が query_id を先渡しする（report の Q 連番 namespace）．"""

    query_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._-]+$")
    text: str = Field(min_length=1, max_length=500)
    role: Literal["overview", "primary", "limitations", "gap"]
    reason: str = Field(min_length=1, max_length=500)

    @field_validator("text", "reason")
    @classmethod
    def readable(cls, value: str) -> str:
        normalized = unicodedata.normalize("NFKC", value)
        if not " ".join(normalized.split()) or len(normalized) != len(value):
            raise ValueError("must be single-line readable text")
        return value

    @classmethod
    def from_query(cls, query: Query, *, query_id: str) -> JobQuery:
        return cls(query_id=query_id, text=query.text, role=query.role, reason=query.reason)


class SearchJobInput(ContractModel):
    mode: Literal["search"]
    topic: str = Field(min_length=1, max_length=500)
    round_index: int = Field(ge=0, le=8)
    queries: list[JobQuery] = Field(min_length=1, max_length=10)


class ExtractJobInput(ContractModel):
    mode: Literal["extract"]
    topic: str = Field(min_length=1, max_length=500)
    round_index: int = Field(ge=0, le=8)
    candidates: list[Candidate] = Field(min_length=1, max_length=30)

    @field_validator("candidates")
    @classmethod
    def unique_urls(cls, value: list[Candidate]) -> list[Candidate]:
        urls = [candidate.url for candidate in value]
        if len(set(urls)) != len(urls):
            raise ValueError("duplicate candidate url")
        return value


JobInput = Annotated[SearchJobInput | ExtractJobInput, Field(discriminator="mode")]


class SubmitJob(ContractModel):
    contract: Literal[CONTRACT_VERSION]
    submit_key: str = Field(min_length=1, max_length=200)
    parent_job_id: str | None = Field(default=None, min_length=8, max_length=128)
    kind: Literal[JOB_KIND]
    input: JobInput
    target_worker_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._-]+$")
    budget: Limits
    deadline_seconds: float = Field(gt=0, le=3600)


class JobEnvelope(ContractModel):
    """submit 応答・job view の全体形（manager 側で job_id / hash / created_at を付加）．"""

    job_id: str = Field(min_length=8, max_length=128)
    contract: Literal[CONTRACT_VERSION]
    submit_key: str = Field(min_length=1, max_length=200)
    parent_job_id: str | None = Field(default=None, min_length=8, max_length=128)
    kind: Literal[JOB_KIND]
    mode: Literal["search", "extract"]
    input: JobInput
    target_worker_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._-]+$")
    budget: Limits
    deadline_seconds: float = Field(gt=0, le=3600)
    input_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    capabilities: list[str] = Field(default_factory=list)
    created_at: float


class WorkerRegistration(ContractModel):
    contract: Literal[CONTRACT_VERSION]
    worker_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._-]+$")
    capabilities: list[str] = Field(min_length=1, max_length=32)
    implementation_version: str = Field(min_length=1, max_length=128)
    os: str = Field(min_length=1, max_length=64)
    architecture: str = Field(min_length=1, max_length=64)
    concurrency: int = Field(ge=1, le=16)
    state: Literal["online", "offline"] = "online"

    @field_validator("capabilities")
    @classmethod
    def must_collect(cls, value: list[str]) -> list[str]:
        if COLLECT_CAPABILITY not in value:
            raise ValueError(f"capabilities must include {COLLECT_CAPABILITY}")
        return value


class Attempt(ContractModel):
    job_id: str
    attempt_id: str
    worker_id: str
    lease_token: str = Field(exclude=True)
    lease_expires_at: float
    attempt_number: int = Field(ge=1)


class JobUsage(ContractModel):
    search_requests: int = Field(default=0, ge=0)
    search_credits: int = Field(default=0, ge=0)
    fetch_requests: int = Field(default=0, ge=0)
    document_bytes: int = Field(default=0, ge=0)
    model_requests: int = Field(default=0, ge=0)


class SearchDiagnostic(ContractModel):
    engine: str = Field(default="unknown", min_length=1, max_length=64)
    unresponsive: bool = False
    reasons: list[str] = Field(default_factory=list, max_length=8)
    result_count: int = Field(default=0, ge=0)
    code: str | None = Field(default=None, min_length=1, max_length=128)


class QueryResult(ContractModel):
    query_id: str = Field(min_length=1, max_length=64)
    text: str = Field(min_length=1, max_length=500)
    role: Literal["overview", "primary", "limitations", "gap"]
    reason: str = Field(min_length=1, max_length=500)
    status: Literal["ok", "failed", "limit"]
    result_count: int = Field(ge=0)
    diagnostic: SearchDiagnostic


class Failure(ContractModel):
    stage: Literal["search", "fetch", "worker", "artifact"]
    query_id: str | None = Field(default=None, min_length=1, max_length=64)
    source_id: str | None = Field(default=None, min_length=1, max_length=64)
    code: str = Field(min_length=1, max_length=128)
    detail: str | None = Field(default=None, min_length=1, max_length=1000)


class ArtifactRef(ContractModel):
    artifact_id: str = Field(min_length=8, max_length=128, pattern=r"^[A-Za-z0-9._-]+$")
    name: str = Field(min_length=1, max_length=200)
    media_type: str = Field(min_length=3, max_length=128)
    bytes: int = Field(ge=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    schema_version: int = Field(ge=1, le=16)


class SearchPayload(ContractModel):
    mode: Literal["search"]
    queries: list[QueryResult] = Field(min_length=1)
    candidates: list[Candidate]
    failures: list[Failure] = Field(default_factory=list)


class ExtractPayload(ContractModel):
    mode: Literal["extract"]
    sources: list[Source]
    failures: list[Failure] = Field(default_factory=list)


ResultPayload = Annotated[SearchPayload | ExtractPayload, Field(discriminator="mode")]


class ResultEnvelope(ContractModel):
    """worker の finalize 結果パック（result-pack.json の本体）．"""

    contract: Literal[CONTRACT_VERSION]
    job_id: str = Field(min_length=8, max_length=128)
    attempt_id: str = Field(min_length=8, max_length=128)
    status: Literal["succeeded", "partial", "failed", "cancelled"]
    stop_reason: str | None = Field(default=None, min_length=1, max_length=128)
    usage: JobUsage
    elapsed_seconds: float = Field(ge=0, le=86_400)
    payload: ResultPayload
    manifest: list[ArtifactRef] = Field(default_factory=list)
