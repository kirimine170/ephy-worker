"""Synthetic preparation tests, not model performance or native adapter evidence."""

import copy
import hashlib
import json
import traceback
from pathlib import Path
from unittest.mock import patch

import pytest

from ephy_worker.capability_gap import (
    INPUT_SCHEMA,
    RULESET,
    GapInputError,
    ValidatedInputs,
    derive_gap_candidates,
    project_observations,
    validate_artifacts,
)

FIXTURES = json.loads(
    (Path(__file__).parent / "fixtures/capability_gap/cases.json").read_text(encoding="utf-8")
)


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def pack(payloads):
    artifacts, entries = {}, []
    for payload in payloads:
        raw = encoded(payload)
        artifact_id = payload["artifact_id"]
        artifacts[artifact_id] = raw
        entries.append({
            "artifact_id": artifact_id, "job_id": payload["job_id"], "run_id": payload["run_id"],
            "sha256": hashlib.sha256(raw).hexdigest(), "byte_length": len(raw), "input_schema": INPUT_SCHEMA,
        })
    return encoded({"schema_version": 1, "artifacts": entries}), artifacts


def case(name="validation_failure"):
    return copy.deepcopy(next(item for item in FIXTURES["cases"] if item["name"] == name))


def inputs(name="validation_failure"):
    return pack(case(name)["payloads"])


def report(name="validation_failure"):
    return derive_gap_candidates(validate_artifacts(*inputs(name)))


def replace_raw(manifest, artifacts, raw):
    spec = json.loads(manifest)
    entry = spec["artifacts"][0]
    artifacts = dict(artifacts)
    artifacts[entry["artifact_id"]] = raw
    entry.update(sha256=hashlib.sha256(raw).hexdigest(), byte_length=len(raw))
    return encoded(spec), artifacts


@pytest.mark.parametrize("fixture", FIXTURES["cases"], ids=lambda value: value["name"])
def test_fixed_synthetic_cases(fixture):
    result = derive_gap_candidates(validate_artifacts(*pack(fixture["payloads"])))
    assert [item["category"] for item in result["gap_candidates"]] == fixture["categories"]
    assert [item["reason"] for item in result["unknowns"]] == fixture["unknowns"]
    assert result["assessment_status"] == fixture["assessment"]
    assert result["evidence_basis"] == "synthetic_caller_supplied"
    assert result["native_adapters"] == "not_connected"
    assert all(item["status"] == "unverified" for item in result["gap_candidates"] + result["cause_hypotheses"])


def test_hashes_bind_raw_bytes_not_canonical_json():
    manifest, artifacts = inputs()
    raw = next(iter(artifacts.values()))
    modified = {next(iter(artifacts)): raw + b" "}
    spec = json.loads(manifest)
    spec["artifacts"][0]["byte_length"] += 1
    with pytest.raises(GapInputError, match="^sha256_mismatch$"):
        validate_artifacts(encoded(spec), modified)
    fixed = validate_artifacts(*replace_raw(manifest, artifacts, raw + b" "))
    assert project_observations(fixed)["evidence"][0]["raw_sha256"] == hashlib.sha256(raw + b" ").hexdigest()


def test_missing_and_extra_artifacts_are_rejected_atomically():
    manifest, artifacts = inputs()
    with pytest.raises(GapInputError, match="^missing_artifact$"):
        validate_artifacts(manifest, {})
    with pytest.raises(GapInputError, match="^extra_artifact$"):
        validate_artifacts(manifest, {**artifacts, "extra": b"{}"})


@pytest.mark.parametrize("field,value,code", [
    ("schema_version", True, "unsupported_manifest_version"),
    ("schema_version", 2, "unsupported_manifest_version"),
    ("schema_version", "1", "unsupported_manifest_version"),
    ("artifacts", [], "invalid_artifact_count"),
])
def test_manifest_contract_is_strict(field, value, code):
    manifest, artifacts = inputs()
    spec = json.loads(manifest)
    spec[field] = value
    with pytest.raises(GapInputError, match=f"^{code}$"):
        validate_artifacts(encoded(spec), artifacts)


@pytest.mark.parametrize("field,value,code", [
    ("byte_length", True, "invalid_byte_length"),
    ("byte_length", 3.0, "invalid_byte_length"),
    ("byte_length", 0, "invalid_byte_length"),
    ("byte_length", 2_000_000, "invalid_byte_length"),
    ("byte_length", 3, "byte_length_mismatch"),
    ("sha256", "A" * 64, "invalid_sha256"),
    ("sha256", "0" * 64, "sha256_mismatch"),
    ("sha256", True, "invalid_sha256"),
    ("input_schema", "BenchmarkRun.v1", "unsupported_input_schema"),
    ("job_id", "different-job", "identity_mismatch"),
    ("run_id", "different-run", "identity_mismatch"),
])
def test_binding_failures(field, value, code):
    manifest, artifacts = inputs()
    spec = json.loads(manifest)
    spec["artifacts"][0][field] = value
    with pytest.raises(GapInputError, match=f"^{code}$"):
        validate_artifacts(encoded(spec), artifacts)


def test_duplicate_manifest_ids_and_unknown_fields():
    manifest, artifacts = inputs()
    spec = json.loads(manifest)
    spec["artifacts"].append(copy.deepcopy(spec["artifacts"][0]))
    with pytest.raises(GapInputError, match="^duplicate_artifact_id$"):
        validate_artifacts(encoded(spec), artifacts)
    spec["artifacts"].pop()
    spec["private_note"] = "PRIVATE-SENTINEL"
    with pytest.raises(GapInputError, match="^invalid_manifest$"):
        validate_artifacts(encoded(spec), artifacts)


@pytest.mark.parametrize("raw,code", [
    (b'{"schema_version":1,"schema_version":1,"artifacts":[]}', "duplicate_json_key"),
    (b'{"schema_version":NaN,"artifacts":[]}', "nonfinite_json"),
    (b'{"schema_version":Infinity,"artifacts":[]}', "nonfinite_json"),
    (b'{"schema_version":-Infinity,"artifacts":[]}', "nonfinite_json"),
    (b'{"schema_version":1e999,"artifacts":[]}', "nonfinite_json"),
    (b'\xff', "invalid_json"),
    (b'{"PRIVATE-SENTINEL":', "invalid_json"),
])
def test_json_ambiguities_and_nonfinite_values(raw, code):
    with pytest.raises(GapInputError, match=f"^{code}$"):
        validate_artifacts(raw, {})


@pytest.mark.parametrize("raw,code", [
    (b'{"PRIVATE-SENTINEL":1,"PRIVATE-SENTINEL":2}', "duplicate_json_key"),
    (b'{"PRIVATE-SENTINEL":NaN}', "nonfinite_json"),
    (b'{"PRIVATE-SENTINEL":1e999}', "nonfinite_json"),
    (b"[]", "invalid_fields"),
    (b"{}", "invalid_fields"),
])
def test_payload_json_checks_apply_after_hash_binding(raw, code):
    with pytest.raises(GapInputError, match=f"^{code}$"):
        validate_artifacts(*replace_raw(*inputs(), raw))


@pytest.mark.parametrize("field,value,code", [
    ("schema_version", "ephy.capability-observations.v2", "unsupported_input_schema"),
    ("source_family", "benchmark", "unsupported_evidence_basis"),
    ("source_family", "formal", "unsupported_evidence_basis"),
    ("source_family", "research", "unsupported_evidence_basis"),
    ("evidence_kind", "live", "unsupported_evidence_basis"),
    ("evidence_kind", "mock", "unsupported_evidence_basis"),
    ("artifact_id", "other", "identity_mismatch"),
    ("run_id", "other", "identity_mismatch"),
    ("job_id", "other", "identity_mismatch"),
])
def test_native_and_relabelled_inputs_cannot_fall_back(field, value, code):
    manifest, artifacts = inputs()
    payload = json.loads(next(iter(artifacts.values())))
    payload[field] = value
    with pytest.raises(GapInputError, match=f"^{code}$"):
        validate_artifacts(*replace_raw(manifest, artifacts, encoded(payload)))


@pytest.mark.parametrize("field,value,code", [
    ("sequence", True, "invalid_sequence"),
    ("sequence", 2, "invalid_sequence"),
    ("category", "environment", "observation_binding_mismatch"),
    ("stage", "setup", "observation_binding_mismatch"),
    ("code", "PRIVATE-SENTINEL", "unsupported_observation_code"),
    ("status", "false", "invalid_status"),
    ("validation_passed", "false", "invalid_validation"),
    ("validation_passed", 0, "invalid_validation"),
    ("validation_passed", None, "outcome_mismatch"),
    ("validation_passed", True, "outcome_mismatch"),
])
def test_observation_types_and_outcomes_are_consistent(field, value, code):
    fixture = case()
    fixture["payloads"][0]["observations"][0][field] = value
    with pytest.raises(GapInputError, match=f"^{code}$"):
        validate_artifacts(*pack(fixture["payloads"]))


def test_missing_observation_fields_are_not_defaulted():
    for field in case()["payloads"][0]["observations"][0]:
        fixture = case()
        del fixture["payloads"][0]["observations"][0][field]
        with pytest.raises(GapInputError, match="^invalid_fields$"):
            validate_artifacts(*pack(fixture["payloads"]))


def test_non_validation_event_cannot_claim_validation():
    fixture = case("timeout")
    fixture["payloads"][0]["observations"][0]["validation_passed"] = False
    with pytest.raises(GapInputError, match="^outcome_mismatch$"):
        validate_artifacts(*pack(fixture["payloads"]))


def test_check_identity_cannot_change_meaning_within_or_across_artifacts():
    fixture = case("conflict")
    second = fixture["payloads"][0]["observations"][1]
    second.update(code="environment_unavailable", category="environment", stage="setup", validation_passed=None)
    with pytest.raises(GapInputError, match="^contradictory_check_binding$"):
        validate_artifacts(*pack(fixture["payloads"]))
    payload = fixture["payloads"][0]
    other = copy.deepcopy(payload)
    other.update(artifact_id="second", observations=[{**second, "sequence": 0}])
    payload["observations"].pop()
    with pytest.raises(GapInputError, match="^contradictory_check_binding$"):
        validate_artifacts(*pack([payload, other]))


def test_conflict_is_preserved_and_does_not_generate_a_diagnosis():
    result = report("conflict")
    assert [item["status"] for item in result["observations"]] == ["failed", "passed"]
    assert result["gap_candidates"] == result["cause_hypotheses"] == []
    assert result["unknowns"][0]["observation_ids"] == [
        item["observation_id"] for item in result["observations"]
    ]


def test_unknown_is_not_a_failure_and_timeout_is_not_a_model_cause():
    unknown = report("unknown_validation")
    assert unknown["observations"][0]["validation_passed"] is None
    assert not unknown["gap_candidates"]
    timeout = report("timeout")
    assert timeout["gap_candidates"][0]["category"] == "workflow"
    assert [item["cause_code"] for item in timeout["cause_hypotheses"]] == ["execution_or_budget_constraint"]
    assert "model" not in encoded(timeout).decode()
    environment = report("environment_failure")
    assert environment["gap_candidates"][0]["category"] == "environment"


def test_receipt_construction_and_mutation_do_not_bypass_validation():
    manifest, artifacts = inputs()
    receipt = ValidatedInputs(manifest, tuple(artifacts.items()))
    for function in (project_observations, derive_gap_candidates):
        with pytest.raises(GapInputError, match="^byte_length_mismatch$"):
            function(ValidatedInputs(manifest, ((next(iter(artifacts)), b"{}"),)))
        with pytest.raises(GapInputError, match="^duplicate_artifact_id$"):
            function(ValidatedInputs(manifest, receipt.artifacts * 2))
        with pytest.raises(GapInputError, match="^invalid_receipt$"):
            function({"manifest_bytes": manifest, "artifacts": artifacts})


def test_determinism_input_immutability_and_output_independence():
    manifest, artifacts = inputs("conflict")
    before = copy.deepcopy(artifacts)
    receipt = validate_artifacts(manifest, artifacts)
    first = derive_gap_candidates(receipt)
    assert encoded(first) == encoded(derive_gap_candidates(receipt))
    assert artifacts == before
    projection = project_observations(receipt)
    projection["observations"][0]["status"] = "passed"
    assert encoded(first) == encoded(derive_gap_candidates(receipt))
    artifacts.clear()
    assert encoded(first) == encoded(derive_gap_candidates(receipt))


def test_observation_and_manifest_order_are_preserved():
    fixture = case("conflict")
    payload = fixture["payloads"][0]
    payload["observations"].reverse()
    for index, obs in enumerate(payload["observations"]):
        obs["sequence"] = index
    result = derive_gap_candidates(validate_artifacts(*pack([payload])))
    assert [obs["status"] for obs in result["observations"]] == ["passed", "failed"]
    other = case("environment_failure")["payloads"][0]
    other["artifact_id"] = "environment-second"
    other["observations"][0]["check_id"] = "environment-check"
    result = derive_gap_candidates(validate_artifacts(*pack([payload, other])))
    assert [obs["code"] for obs in result["observations"]] == ["validation", "validation", "environment_unavailable"]


def test_private_input_is_not_reflected_in_output_or_exception_traceback():
    fixture = case()
    payload = fixture["payloads"][0]
    for name in ("artifact_id", "job_id", "run_id"):
        payload[name] = "PRIVATE-SENTINEL"
    payload["observations"][0]["check_id"] = "PRIVATE-SENTINEL"
    result = derive_gap_candidates(validate_artifacts(*pack([payload])))
    assert "PRIVATE-SENTINEL" not in encoded(result).decode()
    payload["secret"] = "PRIVATE-SENTINEL"
    try:
        validate_artifacts(*pack([payload]))
    except GapInputError:
        assert "PRIVATE-SENTINEL" not in traceback.format_exc()
    else:
        pytest.fail("private extra input accepted")


def test_pure_pipeline_needs_no_io_clock_random_or_execution():
    manifest, artifacts = inputs()
    def forbidden(*args, **kwargs):
        raise AssertionError("unexpected external effect")
    with patch("builtins.open", forbidden), patch("pathlib.Path.open", forbidden), \
         patch("socket.socket", forbidden), patch("subprocess.Popen", forbidden), \
         patch("time.time", forbidden), patch("os.urandom", forbidden):
        result = derive_gap_candidates(validate_artifacts(manifest, artifacts))
    assert result["gap_candidates"]


def test_unknown_ruleset_does_not_silently_change_rules():
    with pytest.raises(GapInputError, match="^unsupported_ruleset$"):
        derive_gap_candidates(validate_artifacts(*inputs()), ruleset=RULESET + "-other")

@pytest.mark.parametrize("mutate,code", [
    ("artifact_count", "invalid_artifact_count"),
    ("observation_count", "invalid_observations"),
    ("manifest_bytes", "invalid_bytes"),
    ("payload_not_bytes", "byte_length_mismatch"),
])
def test_bounded_input_contract(mutate, code):
    manifest, artifacts = inputs()
    spec = json.loads(manifest)
    if mutate == "artifact_count":
        spec["artifacts"] *= 65
        manifest = encoded(spec)
    elif mutate == "observation_count":
        payload = json.loads(next(iter(artifacts.values())))
        payload["observations"] *= 1025
        manifest, artifacts = replace_raw(manifest, artifacts, encoded(payload))
    elif mutate == "manifest_bytes":
        manifest = manifest.decode()
    else:
        key = next(iter(artifacts))
        artifacts[key] = bytearray(artifacts[key])
    with pytest.raises(GapInputError, match=f"^{code}$"):
        validate_artifacts(manifest, artifacts)


def test_conflicting_outcomes_across_artifacts_remain_unknown():
    fixture = case("conflict")
    first = fixture["payloads"][0]
    second = copy.deepcopy(first)
    second.update(artifact_id="second", observations=[{**first["observations"][1], "sequence": 0}])
    first["observations"].pop()
    result = derive_gap_candidates(validate_artifacts(*pack([first, second])))
    assert len(result["evidence"]) == 2
    assert result["gap_candidates"] == []
    assert result["unknowns"][0]["reason"] == "conflicting_observations"
    assert len(result["unknowns"][0]["evidence_ids"]) == 2


def test_receipt_representation_does_not_expose_retained_input_bytes():
    fixture = case()
    payload = fixture["payloads"][0]
    for name in ("artifact_id", "job_id", "run_id"):
        payload[name] = "PRIVATE-SENTINEL"
    payload["observations"][0]["check_id"] = "PRIVATE-SENTINEL"
    manifest, artifacts = pack([payload])
    receipt = validate_artifacts(manifest, artifacts)
    assert "PRIVATE-SENTINEL" not in repr(receipt)
    assert "PRIVATE-SENTINEL" not in str(receipt)
    # Retention is needed to revalidate; representations are not serialization.
    assert receipt.manifest_bytes == manifest
    assert derive_gap_candidates(receipt)["evidence_basis"] == "synthetic_caller_supplied"
