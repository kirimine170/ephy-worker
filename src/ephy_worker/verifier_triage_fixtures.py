"""Public development controls, never the held-out corpus or its answer key."""
from __future__ import annotations

import json

from .verifier_triage_evaluation import sha256


def development_fixture() -> tuple[bytes, bytes]:
    rows = []
    gold = []
    for index in range(12):
        expected = {"id": "e", "aggregate": "a" * 64, "components": {}}
        observed = {"id": "o", "aggregate": "a" * 64, "components": {}}
        decision, diagnosis = "PASS", "MATCH"
        references = ["e", "o"]
        if index in (0, 2, 4, 6):
            name = {0: "PATH", 2: "pytest_version", 4: "python_executable", 6: "locale"}[index]
            expected["components"][name] = "a" * 64
            observed["components"][name] = "b" * 64
            observed["aggregate"] = "b" * 64
            decision, diagnosis = "STOP", "component:" + name
        elif index in (1, 3, 5):
            if index == 1:
                observed["components"]["PATH"] = "b" * 64
            elif index == 3:
                expected["components"]["PATH"] = "a" * 64
            observed["aggregate"] = "b" * 64
            decision, diagnosis = "STOP", "UNATTRIBUTED"
        evidence = {"expected": expected, "observed": observed}
        if index == 8:
            evidence["ci"] = {"expected_id": "ce", "observed_id": "co",
                              "expected_revision": "current-frozen", "observed_revision": "historical"}
            decision, diagnosis = "STOP", "STALE_CI"
            references = ["ce", "co"]
        identifier = f"dev-case-{index:04d}"
        rows.append({"id": identifier, "input": json.dumps(evidence, separators=(",", ":"))})
        gold.append({"id": identifier, "decision": decision, "diagnosis": diagnosis,
                     "evidence_ids": references})
    return (
        json.dumps(rows, separators=(",", ":")).encode(),
        json.dumps(gold, separators=(",", ":")).encode(),
    )


def development_skill() -> bytes:
    """A hand-written synthetic control, not a Pi-generated candidate."""
    return (
        b"---\nname: synthetic-triage-control\n"
        b"description: Public development fixture only.\n---\n"
        b"Verify current evidence. Stop on aggregate mismatch or stale CI.\n"
        b"Name a component only when expected and observed component hashes both exist and differ.\n"
        b"Use UNATTRIBUTED when an aggregate differs without such a component pair.\n"
    )


def development_manifest() -> dict:
    batch, gold = development_fixture()
    return {
        "kind": "development-only", "batch_sha256": sha256(batch),
        "gold_sha256": sha256(gold), "cases": 12,
        "real_heldout_data": False, "model_calls": 0,
    }
