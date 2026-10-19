"""Offline, proposed-only normalization of persisted research Report 0.1 bytes.

This adapter checks bindings and reuses the existing review guards. It does not
perform research, authenticate a report, or establish improvement efficacy.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter
from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .evidence import apply_review, group_origins
from .schema import ClaimCheck, EvidenceCheck, Report, Review

FieldName = Literal[
    "name",
    "version",
    "license",
    "applicability",
    "evaluation_method",
    "required_resources",
    "existing_implementation_delta",
    "improvement_hypothesis",
    "unresolved_matters",
]
FIELDS = (
    "name",
    "version",
    "license",
    "applicability",
    "evaluation_method",
    "required_resources",
    "existing_implementation_delta",
    "improvement_hypothesis",
    "unresolved_matters",
)
_SUPPORTED = {"supported_primary", "corroborated"}


class DiscoveryInputError(ValueError):
    """A stable code only: never echo untrusted report, paths, or log content."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class CandidateSelection(BaseModel):
    """Caller classification of existing claims; no caller-supplied field values."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    candidate_id: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")]
    claims_by_field: dict[FieldName, list[Annotated[str, Field(min_length=1)]]] = Field(min_length=1)


@dataclass(frozen=True)
class DiscoveryResult:
    """Canonical UTF-8 JSON bytes and their exact hash, without a self-hash field."""

    output_bytes: bytes

    @property
    def sha256(self) -> str:
        return _sha256(self.output_bytes)

    def to_dict(self) -> dict:
        """Return a fresh mutable view; the bound output bytes stay immutable."""
        return json.loads(self.output_bytes)


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise DiscoveryInputError("duplicate_json_key")
        result[key] = value
    return result


def _constant(_value: str) -> None:
    raise DiscoveryInputError("nonfinite_json")


def _check_json(value: object) -> None:
    if isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            raise DiscoveryInputError("invalid_unicode") from None
    elif isinstance(value, float) and not math.isfinite(value):
        raise DiscoveryInputError("nonfinite_json")
    elif isinstance(value, list):
        for item in value:
            _check_json(item)
    elif isinstance(value, dict):
        for key, item in value.items():
            _check_json(key)
            _check_json(item)


def _require_explicit_fields(value: object) -> None:
    # Persisted Report.model_dump() includes all defaults. Do not synthesize
    # timestamps or other absent facts while decoding a hand-written report.
    if isinstance(value, BaseModel):
        if value.model_fields_set != set(type(value).model_fields):
            raise DiscoveryInputError("incomplete_report")
        for name in type(value).model_fields:
            _require_explicit_fields(getattr(value, name))
    elif isinstance(value, list):
        for item in value:
            _require_explicit_fields(item)


def _decode_report(data: bytes, expected_hash: str, expected_job: str) -> Report:
    if type(data) is not bytes:
        raise DiscoveryInputError("invalid_report_bytes")
    if type(expected_hash) is not str or not re.fullmatch(r"[0-9a-f]{64}", expected_hash):
        raise DiscoveryInputError("invalid_expected_hash")
    if _sha256(data) != expected_hash:
        raise DiscoveryInputError("report_hash_mismatch")
    if type(expected_job) is not str or not expected_job.strip():
        raise DiscoveryInputError("invalid_expected_job")
    try:
        raw = json.loads(data.decode("utf-8"), object_pairs_hook=_object, parse_constant=_constant)
        _check_json(raw)
        if not isinstance(raw, dict):
            raise DiscoveryInputError("invalid_report_schema")
        if raw.get("schema_version") != "0.1" or raw.get("workflow_version") != "phase1-v1":
            raise DiscoveryInputError("unsupported_report_version")
        report = Report.model_validate(raw, strict=True)
        _require_explicit_fields(report)
        # Prose validators must not silently rewrite the exact stored claims.
        if report.model_dump(mode="json") != raw:
            raise DiscoveryInputError("report_value_normalization")
    except (UnicodeDecodeError, json.JSONDecodeError, ValidationError, RecursionError, ValueError) as exc:
        if isinstance(exc, DiscoveryInputError):
            raise
        raise DiscoveryInputError("invalid_report_schema") from None
    if report.job_id != expected_job:
        raise DiscoveryInputError("report_job_mismatch")
    if report.state not in {"completed", "partial"}:
        raise DiscoveryInputError("report_not_ready")
    return report


def _unique_index(items: list, attribute: str) -> dict:
    result = {}
    for item in items:
        key = getattr(item, attribute)
        if not key.strip() or key in result:
            raise DiscoveryInputError("ambiguous_report_id")
        result[key] = item
    return result


def _check_references(report: Report) -> tuple[dict, dict, dict, dict]:
    sources = _unique_index(report.sources, "source_id")
    passages = _unique_index([p for s in report.sources for p in s.passages], "passage_id")
    evidence = _unique_index(report.evidence, "evidence_id")
    claims = _unique_index(report.claims, "claim_id")
    groups = _unique_index(report.origin_groups, "group_id")
    members = Counter(sid for group in groups.values() for sid in group.source_ids)
    if any(sid not in sources or count != 1 for sid, count in members.items()):
        raise DiscoveryInputError("ambiguous_origin_membership")
    for source in sources.values():
        owned = {p.passage_id for p in source.passages}
        if len(source.read_passage_ids) != len(set(source.read_passage_ids)):
            raise DiscoveryInputError("ambiguous_read_membership")
        if not set(source.read_passage_ids) <= owned:
            raise DiscoveryInputError("invalid_read_membership")
        for passage in source.passages:
            if passage.source_id != source.source_id:
                raise DiscoveryInputError("invalid_passage_owner")
            if passage.start < 0 or passage.end - passage.start != len(passage.text):
                raise DiscoveryInputError("invalid_passage_bounds")
            if passage.page is not None and passage.page < 1:
                raise DiscoveryInputError("invalid_passage_page")
    owners = Counter(eid for claim in claims.values() for eid in claim.evidence_ids)
    if set(owners) != set(evidence) or any(count != 1 for count in owners.values()):
        raise DiscoveryInputError("ambiguous_evidence_owner")
    for claim in claims.values():
        if not claim.text.strip():
            raise DiscoveryInputError("empty_claim")
        refs = claim.inference_from
        if len(refs) != len(set(refs)) or not set(refs) <= set(claims) or claim.claim_id in refs:
            raise DiscoveryInputError("invalid_inference_reference")
    for item in evidence.values():
        source, passage = sources.get(item.source_id), passages.get(item.passage_id)
        if source is None or passage is None or passage.source_id != item.source_id:
            raise DiscoveryInputError("invalid_evidence_reference")
        start, end = item.quote_start - passage.start, item.quote_end - passage.start
        if (
            not item.quote.strip()
            or start < 0
            or end <= start
            or end > len(passage.text)
            or passage.text[start:end] != item.quote
            or item.page != passage.page
        ):
            raise DiscoveryInputError("invalid_evidence_quote")
    return sources, passages, evidence, claims


def _evidence_issues(item, source) -> list[str]:
    issues = []
    if source.status not in {"ok", "partial"}:
        issues.append("source_not_usable")
    if item.passage_id not in source.read_passage_ids:
        issues.append("passage_not_read")
    if not item.checked:
        issues.append("evidence_not_checked")
    if not item.check_reason or not item.check_reason.strip():
        issues.append("evidence_review_reason_missing")
    if item.conditions_match is not True:
        issues.append("conditions_not_matched")
    if item.is_primary is None:
        issues.append("primary_classification_unknown")
    return issues


def _contradictory_origins(sources: dict, groups: list) -> set[str]:
    declared = {
        sid: group.group_id
        for group in groups
        if group.independence == "confirmed"
        for sid in group.source_ids
    }
    conflicts = set()
    for known in group_origins(list(sources.values())):
        if len({declared[sid] for sid in known.source_ids if sid in declared}) > 1:
            conflicts.update(known.source_ids)
    return conflicts


def _recheck_claim(claim, items: list, sources: dict, groups: list, shared_origins: set[str]):
    # Replaying one claim is sufficient after global ownership validation. There
    # is no inference promotion: only the two stored supported states qualify.
    reasons = []
    if not claim.checked:
        reasons.append("claim_not_checked")
    if claim.status not in _SUPPORTED:
        reasons.append(f"report_claim_{claim.status}")
    if not claim.reason.strip():
        reasons.append("claim_review_reason_missing")
    if not items:
        reasons.append("claim_has_no_evidence")
    if claim.status == "corroborated" and any(item.source_id in shared_origins for item in items):
        reasons.append("known_shared_origin")
    checks = []
    for item in items:
        issues = _evidence_issues(item, sources[item.source_id])
        reasons.extend(issues)
        if issues:
            continue
        # model_construct avoids ProseModel normalization and stage-size caps;
        # the stored models and these exact scalar fields were validated above.
        checks.append(
            EvidenceCheck.model_construct(
                evidence_id=item.evidence_id,
                relation=item.relation,
                conditions_match=item.conditions_match,
                is_primary=item.is_primary,
                reason=item.check_reason,
            )
        )
    if reasons:
        return None, sorted(set(reasons)), items
    check = ClaimCheck.model_construct(
        claim_id=claim.claim_id,
        status=claim.status,
        reason=claim.reason,
        conditions=claim.conditions,
        uncertainty=list(claim.uncertainty),
        evidence_checks=checks,
        inference_from=list(claim.inference_from),
    )
    copied = [item.model_copy(deep=True) for item in items]
    result = apply_review(
        [claim.model_copy(deep=True)], copied, Review.model_construct(checks=[check]), groups
    )[0]
    if result.status not in _SUPPORTED:
        reasons.append(f"review_guard_{result.status}")
    elif result.status != claim.status:
        # Corroboration that collapses to primary support remains unresolved as
        # selected: do not silently replace the stored, stronger status.
        reasons.append("review_status_downgraded")
    return result, reasons, copied


def normalize_evaluation_candidates(
    report_bytes: bytes,
    *,
    expected_report_sha256: str,
    expected_job_id: str,
    selections: list[dict],
) -> DiscoveryResult:
    """Normalize explicit claim selections without IO, query building or execution.

    All fields are reported claims, never an independently verified license,
    suitability assessment or improvement result. Invalid inputs raise safe codes.
    """
    report = _decode_report(report_bytes, expected_report_sha256, expected_job_id)
    sources, passages, evidence, claims = _check_references(report)
    if type(selections) is not list or not selections or any(type(item) is not dict for item in selections):
        raise DiscoveryInputError("invalid_selection")
    try:
        parsed = [CandidateSelection.model_validate(item, strict=True) for item in selections]
    except ValidationError:
        raise DiscoveryInputError("invalid_selection") from None
    if len({item.candidate_id for item in parsed}) != len(parsed):
        raise DiscoveryInputError("duplicate_candidate_id")
    for selection in parsed:
        for ids in selection.claims_by_field.values():
            if not ids or len(ids) != len(set(ids)) or not set(ids) <= set(claims):
                raise DiscoveryInputError("invalid_selected_claims")
    shared_origins = _contradictory_origins(sources, report.origin_groups)
    normalized_claims = {}
    selected_ids = sorted({cid for item in parsed for ids in item.claims_by_field.values() for cid in ids})
    for cid in selected_ids:
        claim = claims[cid]
        items = [evidence[eid] for eid in sorted(claim.evidence_ids)]
        rechecked, reasons, guarded = _recheck_claim(
            claim, items, sources, report.origin_groups, shared_origins
        )
        guarded_by_id = {item.evidence_id: item for item in guarded}
        bindings = []
        for item in items:
            source, passage = sources[item.source_id], passages[item.passage_id]
            bindings.append(
                {
                    "report_sha256": expected_report_sha256,
                    "claim_id": cid,
                    "evidence_id": item.evidence_id,
                    "source_id": item.source_id,
                    "passage_id": item.passage_id,
                    "passage_utf8_sha256": _sha256(passage.text.encode("utf-8")),
                    "passage_start": passage.start,
                    "passage_end": passage.end,
                    "page": passage.page,
                    "quote_start": item.quote_start,
                    "quote_end": item.quote_end,
                    "quote": item.quote,
                    "quote_utf8_sha256": _sha256(item.quote.encode("utf-8")),
                    "quote_match": item.quote_match,
                    "declared_source_body_hash": source.body_hash,
                    "source_body_hash_status": "unverified_complete_extracted_body_not_supplied",
                    "source_status": source.status,
                    "source_limitations": list(source.limitations),
                    "conditions": item.conditions,
                    "conditions_match": item.conditions_match,
                    "reported_relation": item.relation,
                    "guarded_relation": guarded_by_id[item.evidence_id].relation,
                    "is_primary": item.is_primary,
                    "checked": item.checked,
                    "check_reason": item.check_reason,
                    "guarded_check_reason": guarded_by_id[item.evidence_id].check_reason,
                    "issues": _evidence_issues(item, source),
                }
            )
        normalized_claims[cid] = {
            "claim_id": cid,
            "text": claim.text,
            "status": "report_supported" if not reasons else "unresolved",
            "reported_status": claim.status,
            "rechecked_status": rechecked.status if rechecked else None,
            "reason_codes": reasons,
            "report_reason": claim.reason,
            "review_guard_reason": rechecked.reason if rechecked else None,
            "conditions": claim.conditions,
            "uncertainty": list(claim.uncertainty),
            "review_guard_uncertainty": list(rechecked.uncertainty) if rechecked else [],
            "inference_from": list(claim.inference_from),
            "bindings": bindings,
        }
    candidates = []
    for selection in sorted(parsed, key=lambda item: item.candidate_id):
        fields = {}
        for name in FIELDS:
            selected = [normalized_claims[cid] for cid in sorted(selection.claims_by_field.get(name, []))]
            fields[name] = {
                "status": (
                    "unknown"
                    if not selected
                    else "report_supported"
                    if all(c["status"] == "report_supported" for c in selected)
                    else "unresolved"
                ),
                "value": [c["text"] for c in selected] if selected else None,
                "reason_codes": [] if selected else ["not_selected"],
                "claims": selected,
            }
        candidates.append({"candidate_id": selection.candidate_id, "state": "proposed", "fields": fields})
    output = {
        "schema_version": "ephy.evaluation-discovery.v1",
        "state": "proposed",
        "report": {
            "schema_version": report.schema_version,
            "workflow_version": report.workflow_version,
            "job_id": report.job_id,
            "sha256": expected_report_sha256,
            "byte_length": len(report_bytes),
            "state": report.state,
        },
        "support_scope": "report_claim_support_only",
        "field_assignment_status": "caller_selected_not_semantically_verified",
        "authenticity_status": "not_authenticated",
        "information_gaps": list(report.gaps),
        "information_gaps_meaning": "research_information_only_not_capability_gaps",
        "candidates": candidates,
    }
    encoded = json.dumps(output, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return DiscoveryResult(encoded.encode("utf-8") + b"\n")
