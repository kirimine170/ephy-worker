"""Independent offline acceptance fixture for the experimental resume entrypoint.

The current report format has no verifiable execution checkpoint. Until one
exists, an interrupted job must be rejected without replaying model or IO
effects or claiming that its cumulative budget was restored.
"""

import asyncio
import hashlib
import json

import pytest

from ephy_worker.research import ResearchExecutor
from ephy_worker.store import JobStore
from test_research import FakeFetcher, FakeModel, FakeSearch, worker


class InterruptAt(FakeModel):
    def __init__(self, stage):
        super().__init__()
        self.stage = stage

    async def run(self, output_type, prompt, *, stage):
        if stage == self.stage:
            raise asyncio.CancelledError()
        return await super().run(output_type, prompt, stage=stage)


@pytest.mark.parametrize("stage", ["plan", "select", "extract", "review"])
@pytest.mark.asyncio
async def test_interrupted_job_cannot_claim_resume_without_checkpoint(tmp_path, stage):
    first = worker(tmp_path, model=InterruptAt(stage))
    # The mock model does not charge requests, so seed usage from an earlier
    # successful call to exercise the persisted-vs-fresh budget boundary.
    first.budget.counts["model_requests"] = 2
    report = await first.run("offline fixture question")
    assert report.state == "cancelled"

    saved = first.store.directory / "report.json"
    events = first.store.directory / "events.jsonl"
    before_report_hash = hashlib.sha256(saved.read_bytes()).hexdigest()
    before_events = events.read_bytes()
    reopened = JobStore.open_existing(tmp_path, first.store.job_id)
    restored = reopened.load_report()
    mock_model, mock_search, mock_fetcher = FakeModel(), FakeSearch(), FakeFetcher()
    second = ResearchExecutor(
        first.config,
        "fixture",
        reopened,
        model=mock_model,
        search=mock_search,
        fetcher=mock_fetcher,
    )
    # Prior model calls and any search/fetch requests belong to a spent budget.
    # A newly constructed executor starts at zero; it cannot safely continue.
    assert report.metrics["requests"]["model_requests"] >= 1
    assert second.budget.counts["model_requests"] == 0

    with pytest.raises(ValueError, match="resume_checkpoint_unsupported"):
        await second.run(report.question, existing_report=restored)

    assert mock_model.calls == []
    assert mock_search.calls == []
    assert hashlib.sha256(saved.read_bytes()).hexdigest() == before_report_hash
    assert events.read_bytes() == before_events


def test_report_hash_mismatch_is_rejected_before_resume(tmp_path):
    store = JobStore(tmp_path)
    from ephy_worker.schema import Report

    store.save(Report(job_id=store.job_id, question="original", profile_id="fixture"))
    path = store.directory / "report.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["question"] = "tampered but valid"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="report hash mismatch"):
        JobStore.open_existing(tmp_path, store.job_id).load_report()


def test_report_schema_has_no_verifiable_checkpoint_provenance():
    from ephy_worker.schema import Report

    required = {"run_id", "attempt_id", "target_revision", "policy_revision", "checkpoint_stage"}
    assert required.isdisjoint(Report.model_fields)
