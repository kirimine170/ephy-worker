from __future__ import annotations

import copy
import hashlib
import json
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path

from ephy_worker.karte_experiment_consumer import (
    EXPECTED_REFS,
    MANIFEST_REF,
    MAX_ARTIFACT,
    METADATA_REF,
    WORKER_ARTIFACTS,
    ConsumerFailure,
    consume_result,
)


def encode(value: dict) -> bytes:
    return (json.dumps(value, sort_keys=True) + "\n").encode("utf-8")


def sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


class KarteExperimentConsumerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.candidate = "synthetic-candidate-001"
        self.artifacts = {
            f"worker/artifacts/{name}": f"synthetic {name}\n".encode() for name in WORKER_ARTIFACTS
        }
        self.metadata = {
            "adapter_version": "karte.worker-experiment.v1",
            "synthetic_only": True,
            "candidate_id": self.candidate,
            "experiment_id": "audit-synthetic-001",
            "run_id": "job-synthetic-001",
            "attempt_id": "attempt-001",
            "target_commit": "unacquired",
            "environment": "synthetic",
            "model": "unacquired",
            "checker": "synthetic-checker",
            "worker_result": "external_review_pending",
            "observations": ["synthetic evidence"],
            "interpretation": "transport only",
            "halt_reason": "no adoption authorized",
            "project": "ephy",
            "title": "Synthetic report",
            "reported_at": "2026-10-06T00:00:00Z",
        }
        self.manifest = {
            "schema_version": "ephy.evidence-manifest.v1",
            "audit_id": self.metadata["experiment_id"],
            "job_id": self.metadata["run_id"],
            "created_at": "2026-10-06T00:00:00Z",
            "artifacts": [
                {
                    "artifact_id": name,
                    "path": f"artifacts/{name}.txt",
                    "media_type": "text/plain",
                    "producer": "synthetic-test",
                    "size_bytes": len(self.artifacts[f"worker/artifacts/{name}"]),
                    "sha256": sha(self.artifacts[f"worker/artifacts/{name}"]),
                }
                for name in WORKER_ARTIFACTS
            ],
        }
        self.artifacts[METADATA_REF] = encode(self.metadata)
        self.artifacts[MANIFEST_REF] = encode(self.manifest)
        self.record = {
            **{
                key: value
                for key, value in self.metadata.items()
                if key not in {"adapter_version", "synthetic_only", "worker_result"}
            },
            "schema_version": "0.1",
            "state": "experiment",
            "verification": "unverified",
            "patch_sha256": sha(self.artifacts["worker/artifacts/candidate_patch"]),
        }
        self.record["observations"] = [
            "Worker result (unverified): " + self.metadata["worker_result"],
            *self.metadata["observations"],
        ]
        self.proposal = {
            "schema_version": "1.1",
            "candidate_id": self.candidate,
            "operation": "create",
            "target_doc_id": None,
            "target_relative_path": None,
            "base_sha256": None,
            "append_position": None,
            "proposed_frontmatter": {"title": "Synthetic report"},
            "proposed_body": "Synthetic transport report",
            "placement": {
                "project": "ephy",
                "kind": "report",
                "year_month": "2026-10",
                "preferred_filename": "synthetic.md",
                "confidence": 0.9,
                "candidates": [],
                "consultation_required": False,
                "consultation_question": None,
            },
            "source_refs": [],
            "sensitivity": "internal",
            "created_at": "2026-10-06T00:00:00Z",
        }
        self.rebind()

    def rebind(self) -> None:
        self.entries = [
            {"logical_ref": ref, "sha256": sha(data), "size_bytes": len(data)}
            for ref, data in sorted(self.artifacts.items())
        ]
        self.record["evidence"] = [
            {"logical_ref": entry["logical_ref"], "sha256": entry["sha256"]} for entry in self.entries
        ]
        self.binding = {
            "schema_version": "karte.experiment-payload.v1",
            "record": self.record,
            "proposal": self.proposal,
            "entries": self.entries,
        }
        self.payload = encode(self.binding)
        self.pin = sha(self.payload)

    def status(self, phase: str = "prepared") -> dict:
        result = {
            "candidate_id": self.candidate,
            "payload_sha256": self.pin,
            "phase": phase,
            "state": "experiment",
            "verification": "unverified",
            "adopted": False,
        }
        if phase not in {"prepared", "pending"}:
            result["receipt"] = {
                "schema_version": "1.1",
                "candidate_id": self.candidate,
                "result": "accepted" if phase == "report_accepted" else phase,
                "doc_id": sha(("karte-ephy-v1.1:" + self.candidate).encode())
                if phase == "report_accepted"
                else None,
                "relative_path": "content/ephy/report/2026-10/synthetic.md"
                if phase == "report_accepted"
                else None,
                "resulting_sha256": sha(b"synthetic human accepted report")
                if phase == "report_accepted"
                else None,
                "processed_at": "2026-10-06T00:05:00Z",
                "error_code": None,
                "message": None,
            }
        return result

    def consume(self, status: dict | None = None, **kwargs):
        return consume_result(
            encode(self.status() if status is None else status),
            self.payload,
            self.artifacts,
            candidate_id=self.candidate,
            payload_sha256=self.pin,
            **kwargs,
        )

    def test_prepare_publish_status_progress_preserves_review_target(self) -> None:
        prepared = self.consume()
        pending = self.consume(self.status("pending"), previous=prepared.observation)
        accepted = self.consume(self.status("report_accepted"), previous=pending.observation)
        self.assertFalse(prepared.duplicate)
        self.assertEqual(prepared.observation.review_target, accepted.observation.review_target)
        target = accepted.observation.review_target
        self.assertEqual(
            (target.candidate_id, target.payload_sha256, target.patch_sha256),
            (self.candidate, self.pin, sha(self.artifacts["worker/artifacts/candidate_patch"])),
        )
        self.assertEqual(
            (target.experiment_id, target.run_id, target.attempt_id, target.target_commit),
            ("audit-synthetic-001", "job-synthetic-001", "attempt-001", "unacquired"),
        )
        self.assertFalse(accepted.observation.adopted)
        self.assertFalse(accepted.observation.review_ready)

    def test_duplicate_receipt_is_idempotent_and_order_independent(self) -> None:
        status = self.status("report_accepted")
        first = self.consume(status)
        status["receipt"] = dict(reversed(list(status["receipt"].items())))
        retry = self.consume(status, previous=first.observation)
        self.assertTrue(retry.duplicate)
        self.assertEqual(first.observation, retry.observation)

    def test_cancel_latches_and_keeps_late_report_fact(self) -> None:
        cancelled = self.consume(self.status("pending"), cancelled=True)
        late = self.consume(self.status("report_accepted"), previous=cancelled.observation)
        self.assertTrue(late.observation.cancelled)
        self.assertEqual(late.observation.producer_phase, "report_accepted")
        self.assertFalse(late.observation.adopted)
        self.assertTrue(self.consume(self.status("report_accepted"), previous=late.observation).duplicate)

    def test_explicit_cancellation_is_not_a_truthy_string(self) -> None:
        with self.assertRaises(ConsumerFailure):
            self.consume(cancelled="false")

    def test_failed_and_strict_worker_results_never_grant_adoption(self) -> None:
        for label in ("external_review_pending", "halted", "strict_pass"):
            self.metadata["worker_result"] = label
            self.record["observations"] = [
                "Worker result (unverified): " + label,
                *self.metadata["observations"],
            ]
            self.artifacts[METADATA_REF] = encode(self.metadata)
            self.rebind()
            for phase in ("prepared", "pending", "report_accepted", "rejected", "conflict", "invalid"):
                with self.subTest(label=label, phase=phase):
                    observation = self.consume(self.status(phase)).observation
                    self.assertEqual(observation.worker_result, label)
                    self.assertFalse(observation.adopted)
                    self.assertFalse(observation.review_ready)

    def test_schema_mismatch_in_each_transport_layer_is_refused(self) -> None:
        for field in ("schema_version",):
            original = self.binding[field]
            self.binding[field] = "karte.experiment-payload.v2"
            self.payload = encode(self.binding)
            self.pin = sha(self.payload)
            with self.assertRaises(ConsumerFailure):
                self.consume()
            self.binding[field] = original
        for object_ in (self.record, self.proposal):
            original = object_["schema_version"]
            object_["schema_version"] = "999"
            self.rebind()
            with self.assertRaises(ConsumerFailure):
                self.consume()
            object_["schema_version"] = original
        self.manifest["schema_version"] = "ephy.evidence-manifest.v2"
        self.artifacts[MANIFEST_REF] = encode(self.manifest)
        self.rebind()
        with self.assertRaises(ConsumerFailure):
            self.consume()

    def test_receipt_schema_and_identity_mismatch_are_refused(self) -> None:
        for field, value in (
            ("schema_version", "1.0"),
            ("candidate_id", "candidate-other"),
            ("result", "rejected"),
        ):
            with self.subTest(field=field):
                status = self.status("report_accepted")
                status["receipt"][field] = value
                with self.assertRaises(ConsumerFailure):
                    self.consume(status)

    def test_receipt_alone_cannot_replace_complete_payload(self) -> None:
        with self.assertRaises(ConsumerFailure):
            consume_result(
                encode(self.status("report_accepted")["receipt"]),
                self.payload,
                self.artifacts,
                candidate_id=self.candidate,
                payload_sha256=self.pin,
            )

    def test_complete_payload_pin_cannot_be_inferred_from_status(self) -> None:
        status = self.status()
        status["payload_sha256"] = "a" * 64
        with self.assertRaises(ConsumerFailure):
            self.consume(status)
        with self.assertRaises(ConsumerFailure):
            consume_result(
                encode(status),
                self.payload,
                self.artifacts,
                candidate_id=self.candidate,
                payload_sha256="a" * 64,
            )

    def test_missing_artifact_and_changed_artifact_fail_on_duplicate_retry(self) -> None:
        previous = self.consume(self.status("report_accepted")).observation
        for ref in EXPECTED_REFS:
            data = self.artifacts.pop(ref)
            with self.subTest(ref=ref, mutation="missing"), self.assertRaises(ConsumerFailure):
                self.consume(self.status("report_accepted"), previous=previous)
            self.artifacts[ref] = data + b"changed"
            with self.subTest(ref=ref, mutation="changed"), self.assertRaises(ConsumerFailure):
                self.consume(self.status("report_accepted"), previous=previous)
            self.artifacts[ref] = data

    def test_extra_artifact_and_duplicate_entry_are_refused(self) -> None:
        self.artifacts["unexpected"] = b"extra"
        with self.assertRaises(ConsumerFailure):
            self.consume()
        del self.artifacts["unexpected"]
        self.binding["entries"][0] = self.binding["entries"][1]
        self.payload = encode(self.binding)
        self.pin = sha(self.payload)
        with self.assertRaises(ConsumerFailure):
            self.consume()

    def test_changed_metadata_cannot_reuse_candidate_observation(self) -> None:
        previous = self.consume().observation
        self.metadata["interpretation"] = "different immutable experiment"
        self.artifacts[METADATA_REF] = encode(self.metadata)
        self.rebind()
        with self.assertRaises(ConsumerFailure):
            self.consume(previous=previous)

    def test_record_and_metadata_shared_provenance_must_agree_even_with_valid_hashes(self) -> None:
        metadata = copy.deepcopy(self.metadata)
        record = copy.deepcopy(self.record)
        shared = sorted(self.metadata.keys() & self.record.keys())
        for field in shared:
            for side in ("metadata", "record"):
                self.metadata = copy.deepcopy(metadata)
                self.record = copy.deepcopy(record)
                target = self.metadata if side == "metadata" else self.record
                target[field] = ["different observation"] if field == "observations" else "other-" + field
                self.artifacts[METADATA_REF] = encode(self.metadata)
                self.rebind()
                with self.subTest(field=field, side=side), self.assertRaises(ConsumerFailure):
                    self.consume()
        self.metadata = metadata
        self.record = record
        self.artifacts[METADATA_REF] = encode(metadata)
        self.rebind()
        self.assertEqual(self.consume().observation.worker_result, metadata["worker_result"])

    def test_observations_retain_the_producer_worker_result_prefix_and_facts(self) -> None:
        for mutation in ("label", "empty", "non-list", "missing-prefix", "extra"):
            self.setUp()
            if mutation == "label":
                self.metadata["worker_result"] = "strict_pass"
            elif mutation == "empty":
                self.metadata["observations"] = []
            elif mutation == "non-list":
                self.metadata["observations"] = "synthetic evidence"
            elif mutation == "missing-prefix":
                self.record["observations"] = self.metadata["observations"]
            else:
                self.record["observations"].append("substituted extra fact")
            self.artifacts[METADATA_REF] = encode(self.metadata)
            self.rebind()
            with self.subTest(mutation=mutation), self.assertRaises(ConsumerFailure):
                self.consume()

    def test_terminal_receipt_cannot_be_substituted_or_regressed(self) -> None:
        for phase in ("report_accepted", "rejected", "conflict", "invalid"):
            previous = self.consume(self.status(phase)).observation
            for next_phase in (
                "prepared",
                "pending",
                *({"report_accepted", "rejected", "conflict", "invalid"} - {phase}),
            ):
                with self.subTest(phase=phase, next_phase=next_phase), self.assertRaises(ConsumerFailure):
                    self.consume(self.status(next_phase), previous=previous)
            replacement = self.status(phase)
            replacement["receipt"]["processed_at"] = "2026-10-06T00:06:00Z"
            with self.subTest(phase=phase, replacement=True), self.assertRaises(ConsumerFailure):
                self.consume(replacement, previous=previous)

    def test_pending_does_not_regress_to_prepared(self) -> None:
        previous = self.consume(self.status("pending")).observation
        with self.assertRaises(ConsumerFailure):
            self.consume(previous=previous)

    def test_unknown_fields_phases_and_authority_claims_are_refused(self) -> None:
        for field, value in (
            ("phase", "adopted"),
            ("phase", []),
            ("adopted", True),
            ("adopted", 0),
            ("verification", "verified"),
            ("state", "adopted"),
            ("unknown", "value"),
            ("candidate_id", "candidate-other"),
        ):
            status = self.status()
            status[field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ConsumerFailure):
                self.consume(status)

    def test_malformed_duplicate_and_nonfinite_json_are_refused(self) -> None:
        for raw in (b"{} {}", b"\xff", b"[]", b'{"phase":"pending","phase":"prepared"}', b'{"x":NaN}'):
            with self.subTest(raw=raw), self.assertRaises(ConsumerFailure):
                consume_result(
                    raw, self.payload, self.artifacts, candidate_id=self.candidate, payload_sha256=self.pin
                )

    def test_accepted_receipt_must_be_complete(self) -> None:
        for field, value in (
            ("doc_id", None),
            ("relative_path", None),
            ("resulting_sha256", "invalid"),
            ("processed_at", "2026-99-99T00:00:00Z"),
            ("error_code", "unexpected"),
            ("doc_id", "a" * 64),
            ("relative_path", "content/ephy/report/2026-09/synthetic.md"),
            ("relative_path", "../synthetic.md"),
            ("message", ["malformed"]),
        ):
            status = self.status("report_accepted")
            status["receipt"][field] = value
            with self.subTest(field=field), self.assertRaises(ConsumerFailure):
                self.consume(status)

    def test_report_collision_suffix_matches_bound_document(self) -> None:
        status = self.status("report_accepted")
        prefix = status["receipt"]["doc_id"][:8]
        status["receipt"]["relative_path"] = f"content/ephy/report/2026-10/synthetic--{prefix}.md"
        self.assertFalse(self.consume(status).observation.adopted)
        status["receipt"]["relative_path"] = "content/ephy/report/2026-10/synthetic--deadbeef.md"
        with self.assertRaises(ConsumerFailure):
            self.consume(status)

    def test_record_inventory_and_patch_bindings_are_checked(self) -> None:
        for mutate in (
            lambda: self.record.update(patch_sha256="a" * 64),
            lambda: self.record["evidence"].pop(),
            lambda: self.record["evidence"].append(self.record["evidence"][0]),
        ):
            original = copy.deepcopy(self.record)
            mutate()
            self.payload = encode(self.binding)
            self.pin = sha(self.payload)
            with self.assertRaises(ConsumerFailure):
                self.consume()
            self.record.clear()
            self.record.update(original)

    def test_worker_manifest_wrong_identity_or_inventory_is_refused(self) -> None:
        for field, value in (("job_id", "wrong-job"), ("audit_id", "wrong-audit"), ("artifacts", [])):
            original = self.manifest[field]
            self.manifest[field] = value
            self.artifacts[MANIFEST_REF] = encode(self.manifest)
            self.rebind()
            with self.subTest(field=field), self.assertRaises(ConsumerFailure):
                self.consume()
            self.manifest[field] = original

    def test_live_or_unknown_worker_adapter_is_refused(self) -> None:
        for field, value in (
            ("synthetic_only", False),
            ("adapter_version", "karte.worker-experiment.v2"),
            ("run_id", "wrong-job"),
            ("worker_result", "adopted"),
        ):
            original = self.metadata[field]
            self.metadata[field] = value
            self.artifacts[METADATA_REF] = encode(self.metadata)
            self.rebind()
            with self.subTest(field=field), self.assertRaises(ConsumerFailure):
                self.consume()
            self.metadata[field] = original

    def test_mutable_or_oversize_artifacts_are_refused(self) -> None:
        ref = "worker/artifacts/candidate_patch"
        for data in (bytearray(self.artifacts[ref]), b"x" * (MAX_ARTIFACT + 1)):
            self.artifacts[ref] = data
            with self.subTest(type=type(data).__name__), self.assertRaises(ConsumerFailure):
                self.consume()

    def test_inputs_and_output_are_immutable(self) -> None:
        before = copy.deepcopy((self.binding, self.artifacts))
        result = self.consume()
        self.assertEqual(before, (self.binding, self.artifacts))
        with self.assertRaises(FrozenInstanceError):
            result.observation.cancelled = True
        with self.assertRaises(FrozenInstanceError):
            result.observation.review_target.patch_sha256 = "a" * 64

    def test_supported_inventory_matches_repository_manifest_schema(self) -> None:
        root = Path(__file__).resolve().parents[1]
        schema = json.loads(
            (
                root / ".agents/skills/ephy-worker-self-improvement/references/evidence-manifest.schema.json"
            ).read_bytes()
        )
        names = tuple(
            schema["$defs"][entry["$ref"].split("/")[-1]]["properties"]["artifact_id"]["const"]
            for entry in schema["properties"]["artifacts"]["prefixItems"]
        )
        self.assertEqual(WORKER_ARTIFACTS, names)


if __name__ == "__main__":
    unittest.main()
