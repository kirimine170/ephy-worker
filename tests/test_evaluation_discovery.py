"""Synthetic controls for the offline Evaluation Discovery adapter."""

import copy
import hashlib
import json

import pytest

from ephy_worker.evaluation_discovery import (
    CandidateSelection,
    DiscoveryInputError,
    normalize_evaluation_candidates,
)
from ephy_worker.schema import Claim, Evidence, OriginGroup, Passage, Report, Source

STAMP = "2026-10-11T00:00:00+00:00"
TEXT = "Synthetic Check v2 measures parsing accuracy on Linux."


def fixture_report():
    passage = Passage(passage_id="p1", source_id="s1", text=TEXT, start=40, end=40 + len(TEXT))
    source = Source(
        source_id="s1",
        requested_url="https://example.invalid/synthetic-check",
        status="ok",
        fetched_at=STAMP,
        body_hash="declared-extracted-body-hash",
        passages=[passage],
        read_passage_ids=["p1"],
        limitations=["Only the supplied synthetic passage was read."],
    )
    item = Evidence(
        evidence_id="e1",
        source_id="s1",
        passage_id="p1",
        quote=TEXT,
        quote_start=40,
        quote_end=40 + len(TEXT),
        relation="supports",
        checked=True,
        check_reason="The synthetic claim matches this exact passage.",
        conditions="Linux, v2 only",
        conditions_match=True,
        is_primary=True,
    )
    claim = Claim(
        claim_id="c1",
        text=TEXT,
        evidence_ids=["e1"],
        status="supported_primary",
        checked=True,
        reason="Synthetic primary source support.",
        conditions="Linux, v2 only",
        uncertainty=["Other platforms were not checked."],
    )
    return Report(
        job_id="synthetic-job",
        question="Public synthetic evaluation candidates",
        profile_id="synthetic-no-model",
        created_at=STAMP,
        finished_at=STAMP,
        state="completed",
        sources=[source],
        evidence=[item],
        claims=[claim],
        gaps=["Comparison with the local implementation was not investigated."],
    ).model_dump(mode="json")


def encode(report):
    return json.dumps(report, ensure_ascii=False).encode("utf-8")


def selection(**fields):
    return [{"candidate_id": "synthetic-check", "claims_by_field": fields or {"name": ["c1"]}}]


def normalize(report=None, selections=None):
    data = encode(fixture_report() if report is None else report)
    return normalize_evaluation_candidates(
        data,
        expected_report_sha256=hashlib.sha256(data).hexdigest(),
        expected_job_id="synthetic-job",
        selections=selection() if selections is None else selections,
    )


def claim_result(result):
    return result.to_dict()["candidates"][0]["fields"]["name"]["claims"][0]


def assert_rejected(report, code):
    with pytest.raises(DiscoveryInputError) as error:
        normalize(report)
    assert error.value.code == code
    assert str(error.value) == code


def test_positive_control_is_proposed_and_binds_actual_bytes_and_exact_text():
    raw = fixture_report()
    result = normalize(raw, selection(name=["c1"], evaluation_method=["c1"]))
    output = result.to_dict()
    candidate = output["candidates"][0]
    claim = claim_result(result)
    binding = claim["bindings"][0]
    assert output["state"] == candidate["state"] == "proposed"
    assert candidate["fields"]["name"]["value"] == [TEXT]
    assert claim["status"] == "report_supported"
    assert output["report"]["sha256"] == hashlib.sha256(encode(raw)).hexdigest()
    assert binding["report_sha256"] == output["report"]["sha256"]
    assert binding["claim_id"] == "c1"
    assert binding["source_id"] == "s1"
    assert binding["passage_id"] == "p1"
    assert binding["evidence_id"] == "e1"
    assert binding["quote_start"] == binding["passage_start"] == 40
    assert binding["quote_end"] == binding["passage_end"] == 40 + len(TEXT)
    assert binding["quote"] == TEXT
    assert binding["passage_utf8_sha256"] == binding["quote_utf8_sha256"]
    assert binding["quote_utf8_sha256"] == hashlib.sha256(TEXT.encode("utf-8")).hexdigest()
    assert binding["declared_source_body_hash"] == "declared-extracted-body-hash"
    assert binding["source_body_hash_status"] == "unverified_complete_extracted_body_not_supplied"
    assert output["authenticity_status"] == "not_authenticated"
    assert output["field_assignment_status"] == "caller_selected_not_semantically_verified"
    assert output["support_scope"] == "report_claim_support_only"


def test_unknown_metadata_is_explicit_without_invented_license_version_or_improvement():
    candidate = normalize().to_dict()["candidates"][0]
    for name, field in candidate["fields"].items():
        if name == "name":
            continue
        assert field == {"status": "unknown", "value": None, "claims": [], "reason_codes": ["not_selected"]}
    assert {"license", "version", "applicability", "required_resources", "evaluation_method"} <= set(
        candidate["fields"]
    )
    assert {"improvement_hypothesis", "existing_implementation_delta", "unresolved_matters"} <= set(
        candidate["fields"]
    )


def test_conditions_uncertainty_and_information_gaps_are_retained_without_capability_inference():
    raw = fixture_report()
    raw["gaps"] = [r"Private C:\synthetic\local.log has not been evaluated."]
    result = normalize(raw)
    claim = claim_result(result)
    assert claim["conditions"] == "Linux, v2 only"
    assert claim["uncertainty"] == ["Other platforms were not checked."]
    assert claim["bindings"][0]["conditions"] == "Linux, v2 only"
    assert claim["bindings"][0]["source_limitations"] == raw["sources"][0]["limitations"]
    assert result.to_dict()["information_gaps"] == raw["gaps"]
    assert result.to_dict()["information_gaps_meaning"] == "research_information_only_not_capability_gaps"
    assert "queries" not in result.to_dict()


def test_deterministic_serialization_sorting_hash_and_input_immutability():
    raw = fixture_report()
    selections = [
        {"candidate_id": "z", "claims_by_field": {"name": ["c1"]}},
        {"candidate_id": "a", "claims_by_field": {"license": ["c1"], "name": ["c1"]}},
    ]
    before = copy.deepcopy((raw, selections))
    first, second = normalize(raw, selections), normalize(raw, list(reversed(selections)))
    assert first == second
    assert first.sha256 == hashlib.sha256(first.output_bytes).hexdigest()
    assert first.output_bytes.endswith(b"\n")
    assert first.output_bytes == (
        json.dumps(first.to_dict(), sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
        + b"\n"
    )
    view = first.to_dict()
    view["state"] = "adopted"
    assert first.to_dict()["state"] == "proposed"
    assert (raw, selections) == before


@pytest.mark.parametrize(
    ("location", "field", "value", "reason"),
    [
        ("claims", "checked", False, "claim_not_checked"),
        ("claims", "status", "insufficient", "report_claim_insufficient"),
        ("claims", "status", "refuted", "report_claim_refuted"),
        ("claims", "status", "conflicting", "report_claim_conflicting"),
        ("claims", "status", "inference", "report_claim_inference"),
        ("claims", "reason", "", "claim_review_reason_missing"),
        ("evidence", "checked", False, "evidence_not_checked"),
        ("evidence", "check_reason", None, "evidence_review_reason_missing"),
        ("evidence", "conditions_match", False, "conditions_not_matched"),
        ("evidence", "conditions_match", None, "conditions_not_matched"),
        ("evidence", "is_primary", None, "primary_classification_unknown"),
        ("sources", "status", "blocked", "source_not_usable"),
        ("sources", "read_passage_ids", [], "passage_not_read"),
    ],
)
def test_forged_checked_flag_does_not_override_missing_support(location, field, value, reason):
    raw = fixture_report()
    raw[location][0][field] = value
    claim = claim_result(normalize(raw))
    assert claim["status"] == "unresolved"
    assert reason in claim["reason_codes"]
    assert claim["text"] == TEXT


@pytest.mark.parametrize("relation", ["context_only", "contradicts"])
def test_non_supporting_relations_are_never_supported(relation):
    raw = fixture_report()
    raw["evidence"][0]["relation"] = relation
    claim = claim_result(normalize(raw))
    assert claim["status"] == "unresolved"
    assert claim["bindings"][0]["reported_relation"] == relation


@pytest.mark.parametrize(
    ("quote", "text"),
    [
        ("The model has 30.5B parameters.", "The model has 30B parameters."),
        ("The models are comparable.", "The model outperforms the baseline."),
    ],
)
def test_existing_numeric_and_comparison_guards_are_reused(quote, text):
    raw = fixture_report()
    raw["sources"][0]["passages"][0].update(text=quote, end=40 + len(quote))
    raw["evidence"][0].update(quote=quote, quote_end=40 + len(quote))
    raw["claims"][0]["text"] = text
    claim = claim_result(normalize(raw))
    assert claim["status"] == "unresolved"
    assert claim["rechecked_status"] == "insufficient"
    assert claim["bindings"][0]["guarded_relation"] == "context_only"
    assert claim["review_guard_uncertainty"]


def add_second_evidence(raw, *, relation="supports", primary=False):
    source = copy.deepcopy(raw["sources"][0])
    source.update(
        source_id="s2",
        read_passage_ids=["p2"],
        requested_url="https://second.invalid/control",
        body_hash="different-declared-hash",
    )
    source["passages"][0].update(source_id="s2", passage_id="p2")
    evidence = copy.deepcopy(raw["evidence"][0])
    evidence.update(source_id="s2", passage_id="p2", evidence_id="e2", relation=relation, is_primary=primary)
    raw["sources"].append(source)
    raw["evidence"].append(evidence)
    raw["claims"][0]["evidence_ids"].append("e2")


def test_conflicting_evidence_cannot_be_hidden_by_supported_claim_status():
    raw = fixture_report()
    add_second_evidence(raw, relation="contradicts")
    claim = claim_result(normalize(raw))
    assert claim["rechecked_status"] == "conflicting"
    assert claim["status"] == "unresolved"
    assert len(claim["bindings"]) == 2


def test_primary_and_independent_corroboration_requirements():
    raw = fixture_report()
    raw["evidence"][0]["is_primary"] = False
    assert claim_result(normalize(raw))["status"] == "unresolved"
    add_second_evidence(raw)
    raw["claims"][0]["status"] = "corroborated"
    raw["origin_groups"] = [
        OriginGroup(
            group_id=f"g{i}", source_ids=[f"s{i}"], reasons=["Synthetic control"], independence="confirmed"
        ).model_dump()
        for i in (1, 2)
    ]
    assert claim_result(normalize(raw))["status"] == "report_supported"
    raw["origin_groups"][1]["independence"] = "unknown"
    assert claim_result(normalize(raw))["status"] == "unresolved"
    raw["evidence"][0]["is_primary"] = True
    claim = claim_result(normalize(raw))
    assert claim["rechecked_status"] == "supported_primary"
    assert claim["reason_codes"] == ["review_status_downgraded"]


def test_whitespace_normalized_marker_still_requires_stored_exact_slice():
    raw = fixture_report()
    quote = "Synthetic Check\n v2 measures parsing accuracy on Linux."
    raw["sources"][0]["passages"][0].update(text=quote, end=40 + len(quote))
    raw["evidence"][0].update(quote=quote, quote_end=40 + len(quote), quote_match="whitespace_normalized")
    assert claim_result(normalize(raw))["status"] == "report_supported"
    raw["evidence"][0]["quote"] = TEXT
    assert_rejected(raw, "invalid_evidence_quote")


@pytest.mark.parametrize(
    ("field", "value"),
    [("quote", "Forged quote"), ("quote_start", 39), ("quote_end", 999), ("page", 1)],
)
def test_altered_quotes_bounds_and_page_rejected(field, value):
    raw = fixture_report()
    raw["evidence"][0][field] = value
    assert_rejected(raw, "invalid_evidence_quote")


@pytest.mark.parametrize("collection", ["sources", "claims", "evidence"])
def test_duplicate_report_ids_rejected(collection):
    raw = fixture_report()
    raw[collection].append(copy.deepcopy(raw[collection][0]))
    assert_rejected(raw, "ambiguous_report_id")


def test_duplicate_passage_id_and_wrong_owner_rejected():
    raw = fixture_report()
    raw["sources"][0]["passages"].append(copy.deepcopy(raw["sources"][0]["passages"][0]))
    assert_rejected(raw, "ambiguous_report_id")
    raw = fixture_report()
    raw["sources"][0]["passages"][0]["source_id"] = "other"
    assert_rejected(raw, "invalid_passage_owner")


@pytest.mark.parametrize("mode", ["shared", "duplicate", "missing", "orphan"])
def test_evidence_must_have_exactly_one_claim_owner(mode):
    raw = fixture_report()
    if mode == "shared":
        second = copy.deepcopy(raw["claims"][0])
        second["claim_id"] = "c2"
        raw["claims"].append(second)
    elif mode == "duplicate":
        raw["claims"][0]["evidence_ids"].append("e1")
    elif mode == "missing":
        raw["claims"][0]["evidence_ids"] = ["missing"]
    else:
        raw["claims"][0]["evidence_ids"] = []
    assert_rejected(raw, "ambiguous_evidence_owner")


@pytest.mark.parametrize(
    ("change", "code"),
    [
        (lambda r: r["sources"][0]["read_passage_ids"].append("missing"), "invalid_read_membership"),
        (lambda r: r["sources"][0]["read_passage_ids"].append("p1"), "ambiguous_read_membership"),
        (lambda r: r["sources"][0]["passages"][0].update(end=0), "invalid_passage_bounds"),
        (lambda r: r["sources"][0]["passages"][0].update(page=0), "invalid_passage_page"),
        (lambda r: r["evidence"][0].update(source_id="missing"), "invalid_evidence_reference"),
        (lambda r: r["evidence"][0].update(passage_id="missing"), "invalid_evidence_reference"),
        (lambda r: r["claims"][0].update(inference_from=["missing"]), "invalid_inference_reference"),
        (lambda r: r["claims"][0].update(inference_from=["c1"]), "invalid_inference_reference"),
    ],
)
def test_structurally_invalid_bindings_rejected(change, code):
    raw = fixture_report()
    change(raw)
    assert_rejected(raw, code)


def test_overlapping_origin_membership_is_ambiguous():
    raw = fixture_report()
    raw["origin_groups"] = [
        OriginGroup(group_id=f"g{i}", source_ids=["s1"], reasons=[]).model_dump() for i in (1, 2)
    ]
    assert_rejected(raw, "ambiguous_origin_membership")


@pytest.mark.parametrize("state", ["running", "failed", "cancelled"])
def test_incomplete_or_failed_job_not_accepted(state):
    raw = fixture_report()
    raw["state"] = state
    assert_rejected(raw, "report_not_ready")


def test_partial_report_can_retain_supported_claims():
    raw = fixture_report()
    raw["state"] = "partial"
    assert claim_result(normalize(raw))["status"] == "report_supported"


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (lambda r: r.pop("schema_version"), "unsupported_report_version"),
        (lambda r: r.update(schema_version="99"), "unsupported_report_version"),
        (lambda r: r.update(workflow_version="phase-new"), "unsupported_report_version"),
        (lambda r: r.pop("created_at"), "incomplete_report"),
        (lambda r: r["sources"][0].pop("fetched_at"), "incomplete_report"),
        (lambda r: r["evidence"][0].pop("checked"), "incomplete_report"),
        (lambda r: r["evidence"][0].update(checked="true"), "invalid_report_schema"),
        (lambda r: r["evidence"][0].update(quote_start=True), "invalid_report_schema"),
        (lambda r: r.update(unexpected="private content"), "invalid_report_schema"),
        (lambda r: r["claims"][0].update(text="原文。"), "report_value_normalization"),
    ],
)
def test_strict_schema_no_coercion_or_missing_defaults(mutate, code):
    raw = fixture_report()
    mutate(raw)
    assert_rejected(raw, code)


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity", "1e309"])
def test_nonfinite_numbers_in_arbitrary_nested_report_fields_rejected(literal):
    raw = encode(fixture_report()).replace(
        b'"metrics": {}', ('"metrics": {"nested": [' + literal + "]}").encode()
    )
    with pytest.raises(DiscoveryInputError, match="^nonfinite_json$"):
        normalize_evaluation_candidates(
            raw,
            expected_report_sha256=hashlib.sha256(raw).hexdigest(),
            expected_job_id="synthetic-job",
            selections=selection(),
        )


@pytest.mark.parametrize("nested", [False, True])
def test_duplicate_json_keys_are_rejected_at_every_depth(nested):
    data = encode(fixture_report())
    if nested:
        data = data.replace(b'"metrics": {}', b'"metrics": {"private": 1, "private": 2}')
    else:
        data = data.replace(b'"job_id":', b'"job_id": "other", "job_id":', 1)
    with pytest.raises(DiscoveryInputError, match="^duplicate_json_key$"):
        normalize_evaluation_candidates(
            data,
            expected_report_sha256=hashlib.sha256(data).hexdigest(),
            expected_job_id="synthetic-job",
            selections=selection(),
        )


@pytest.mark.parametrize("hash_value", [None, "", "abc", "A" * 64, 123])
def test_missing_or_invalid_expected_hash_rejected(hash_value):
    with pytest.raises(DiscoveryInputError, match="^invalid_expected_hash$"):
        normalize_evaluation_candidates(
            encode(fixture_report()),
            expected_report_sha256=hash_value,
            expected_job_id="synthetic-job",
            selections=selection(),
        )


def test_exact_hash_changes_with_whitespace_and_job_must_match():
    data = encode(fixture_report())
    with pytest.raises(DiscoveryInputError, match="^report_hash_mismatch$"):
        normalize_evaluation_candidates(
            data + b" ",
            expected_report_sha256=hashlib.sha256(data).hexdigest(),
            expected_job_id="synthetic-job",
            selections=selection(),
        )
    with pytest.raises(DiscoveryInputError, match="^report_job_mismatch$"):
        normalize_evaluation_candidates(
            data,
            expected_report_sha256=hashlib.sha256(data).hexdigest(),
            expected_job_id="other",
            selections=selection(),
        )


@pytest.mark.parametrize(
    "selections",
    [
        [],
        None,
        "private-path",
        [{"candidate_id": "x", "claims_by_field": {"license": ["c1"]}, "value": "MIT"}],
        [{"candidate_id": "x", "claims_by_field": {"license": {"value": "MIT", "claim_id": "c1"}}}],
        [{"candidate_id": "x", "claims_by_field": {"unknown": ["c1"]}}],
        [{"candidate_id": "x", "claims_by_field": {"license": [1]}}],
    ],
)
def test_selection_rejects_arbitrary_field_values_and_untyped_input(selections):
    data = encode(fixture_report())
    with pytest.raises(DiscoveryInputError, match="^invalid_selection$"):
        normalize_evaluation_candidates(
            data,
            expected_report_sha256=hashlib.sha256(data).hexdigest(),
            expected_job_id="synthetic-job",
            selections=selections,
        )


@pytest.mark.parametrize("ids", [[], ["c1", "c1"], ["missing"]])
def test_selected_claim_ids_must_be_nonempty_unique_and_resolve(ids):
    with pytest.raises(DiscoveryInputError, match="^invalid_selected_claims$"):
        normalize(selections=selection(name=ids))


def test_different_metadata_fields_bind_to_their_selected_claims_without_transfer():
    raw = fixture_report()
    second = copy.deepcopy(raw["claims"][0])
    second.update(claim_id="c2", text="License is not established.", evidence_ids=[], status="insufficient")
    raw["claims"].append(second)
    result = normalize(raw, selection(name=["c1"], license=["c2"]))
    fields = result.to_dict()["candidates"][0]["fields"]
    assert fields["name"]["status"] == "report_supported"
    assert fields["license"]["status"] == "unresolved"
    assert fields["license"]["value"] == ["License is not established."]
    assert fields["license"]["claims"][0]["bindings"] == []
    assert fields["version"]["value"] is None


def test_safe_error_never_echoes_private_input():
    raw = fixture_report()
    secret = r"C:\private\credential.log SECRET_TOKEN"
    raw["evidence"][0]["quote_start"] = secret
    with pytest.raises(DiscoveryInputError) as error:
        normalize(raw)
    assert secret not in str(error.value)
    assert str(error.value) == "invalid_report_schema"
    assert error.value.__suppress_context__


def test_lone_surrogate_is_rejected_as_safe_error():
    raw = fixture_report()
    data = json.dumps(raw).replace('"metrics": {}', '"metrics": {"value": "\\ud800"}').encode()
    with pytest.raises(DiscoveryInputError, match="^invalid_unicode$"):
        normalize_evaluation_candidates(
            data,
            expected_report_sha256=hashlib.sha256(data).hexdigest(),
            expected_job_id="synthetic-job",
            selections=selection(),
        )


@pytest.mark.parametrize("relation", ["requested_url", "final_url", "body_hash", "origin_url"])
def test_known_shared_origins_cannot_be_declared_independent(relation):
    raw = fixture_report()
    add_second_evidence(raw)
    raw["evidence"][0]["is_primary"] = False
    raw["claims"][0]["status"] = "corroborated"
    raw["origin_groups"] = [
        OriginGroup(
            group_id=f"g{i}",
            source_ids=[f"s{i}"],
            reasons=["Declared independence"],
            independence="confirmed",
        ).model_dump()
        for i in (1, 2)
    ]
    first, second = raw["sources"]
    if relation == "origin_url":
        first["origin_urls"] = second["origin_urls"] = ["https://origin.invalid/shared"]
    elif relation == "final_url":
        first["final_url"] = second["final_url"] = "https://origin.invalid/shared"
    else:
        second[relation] = first[relation]
    claim = claim_result(normalize(raw))
    assert claim["status"] == "unresolved"
    assert "known_shared_origin" in claim["reason_codes"]


@pytest.mark.parametrize("construct", [False, True])
def test_selection_model_instances_cannot_bypass_raw_dict_contract(construct):
    if construct:
        supplied = CandidateSelection.model_construct(candidate_id="x", claims_by_field={"license": ["c1"]})
    else:
        supplied = CandidateSelection(candidate_id="x", claims_by_field={"license": ["c1"]})
    supplied.claims_by_field["private-input"] = ["c1"]
    with pytest.raises(DiscoveryInputError, match="^invalid_selection$"):
        normalize(selections=[supplied])
