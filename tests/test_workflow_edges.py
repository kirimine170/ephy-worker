"""Regression checks for losing verified history or hanging during cleanup."""

import asyncio
import json

from test_research import FakeFetcher, FakeModel, worker

from ephy_worker.schema import ClaimProposal, EvidenceProposal, Extraction


class HistoryFetcher(FakeFetcher):
    async def fetch(self, candidate, source_id):
        result = await super().fetch(candidate, source_id)
        if source_id == "S4":
            result.passages[0].text = "Version 2 does not support records."
            result.passages[0].end = len(result.passages[0].text)
        return result


class HistoryModel(FakeModel):
    def __init__(self):
        super().__init__(additional=True)
        self.extractions = 0

    async def run(self, output_type, prompt, *, stage):
        data = json.loads(prompt.split("INPUT_DATA_JSON:\n", 1)[1])
        if stage == "extract":
            self.calls.append(stage)
            self.extractions += 1
            if self.extractions == 1:
                selected = [p for p in data["passages"] if p["source_id"] in {"S1", "S4"}]
                title = "Version 2 supports records."
            else:
                selected = [data["passages"][-1]]
                title = "Version 2 has a documented record limit."
            return Extraction(
                claims=[
                    ClaimProposal(
                        text=title,
                        evidence=[
                            EvidenceProposal(
                                source_id=p["source_id"],
                                passage_id=p["passage_id"],
                                quote=p["text"],
                                relation="contradicts" if p["source_id"] == "S4" else "supports",
                            )
                            for p in selected
                        ],
                    )
                ],
                gaps=["初回に発見した矛盾が未解決．"] if self.extractions == 1 else [],
            )
        result = await super().run(output_type, prompt, stage=stage)
        if stage == "review" and self.extractions == 1:
            relations = {e["evidence_id"]: e["relation"] for e in data["evidence"]}
            for check in result.checks:
                for item in check.evidence_checks:
                    item.relation = relations[item.evidence_id]
        return result


async def test_additional_round_cannot_silently_drop_prior_conflict_or_gap(tmp_path):
    w = worker(tmp_path, model=HistoryModel(), fetcher=HistoryFetcher())
    result = await w.run("version 2 の制約")
    assert len(result.rounds) == 2
    initial = next(c for c in result.claims if c.claim_id.startswith("R0-"))
    assert initial.status == "conflicting"
    assert any(c.claim_id.startswith("R1-") for c in result.claims)
    assert "初回に発見した矛盾が未解決．" in result.gaps
    evidence_ids = {e.evidence_id for e in result.evidence}
    assert set(initial.evidence_ids) <= evidence_ids
    markdown = (w.store.directory / "report.md").read_text(encoding="utf-8")
    assert "R0-c001" in markdown and "矛盾が未解決" in markdown
    assert "Version 2 does not support records." in markdown


async def test_hanging_client_close_does_not_prevent_cancelled_report(tmp_path):
    w = worker(tmp_path)
    started = asyncio.Event()

    async def pause(_urls):
        started.set()
        await asyncio.Future()

    async def never_close():
        await asyncio.Future()

    w._execute = pause
    w.model.aclose = never_close
    task = asyncio.create_task(w.run("公開の質問"))
    await started.wait()
    task.cancel()
    result = await asyncio.wait_for(task, timeout=4)
    assert result.state == "cancelled"
    saved = json.loads((w.store.directory / "report.json").read_text(encoding="utf-8"))
    assert saved["state"] == "cancelled"
    assert (w.store.directory / "status.json").exists()


async def test_cancel_new_context_retains_prior_checked_citations(tmp_path, monkeypatch):
    import ephy_worker.evidence as evidence_module

    second_started = asyncio.Event()

    class CancelSecondExtraction(FakeModel):
        def __init__(self):
            super().__init__(additional=True)
            self.extractions = 0

        async def run(self, output_type, prompt, *, stage):
            if stage == "extract":
                self.extractions += 1
                if self.extractions == 2:
                    second_started.set()
                    await asyncio.Future()
            return await super().run(output_type, prompt, stage=stage)

    actual_select = evidence_module.select_passages
    selection_count = 0

    def alternate_context(sources, question, max_passages):
        nonlocal selection_count
        selection_count += 1
        if selection_count == 1:
            return actual_select(sources, question, max_passages)
        return [p for s in sources if s.source_id == "S8" for p in s.passages]

    monkeypatch.setattr(evidence_module, "select_passages", alternate_context)
    w = worker(tmp_path, model=CancelSecondExtraction())
    task = asyncio.create_task(w.run("version 2 の制約"))
    await asyncio.wait_for(second_started.wait(), timeout=5)
    assert w.report.claims[0].checked
    old_evidence = w.report.evidence[0].model_copy(deep=True)
    task.cancel()
    result = await asyncio.wait_for(task, timeout=5)
    assert result.state == "cancelled"
    assert result.claims[0].checked
    original_source = next(s for s in result.sources if s.source_id == old_evidence.source_id)
    assert old_evidence.passage_id in original_source.read_passage_ids
    markdown = (w.store.directory / "report.md").read_text(encoding="utf-8")
    assert "#page=8" in markdown
    assert "整合性警告" not in markdown


async def test_japanese_context_trim_records_only_passages_actually_sent(tmp_path):
    from ephy_worker.models import estimate_tokens
    from ephy_worker.schema import Passage

    class JapaneseFetcher(FakeFetcher):
        async def fetch(self, candidate, source_id):
            result = await super().fetch(candidate, source_id)
            result.passages = [
                Passage(
                    source_id=source_id,
                    passage_id=f"{source_id}-jp-{index}",
                    text="版2の制約はLinuxでのみ動作する点である．" * 60,
                    page=index if result.kind == "pdf" else None,
                    start=(index - 1) * 1600,
                    end=index * 1600,
                )
                for index in range(1, 5)
            ]
            return result

    class ContextCheckingModel(FakeModel):
        def __init__(self):
            super().__init__()
            self.metadata = {
                "model_id": "offline-context-fixture",
                "real_model": False,
                "context_tokens": 16000,
                "max_output_tokens": 1024,
            }
            self.sent_ids = set()

        async def run(self, output_type, prompt, *, stage):
            if stage == "extract":
                schema_size = estimate_tokens(json.dumps(output_type.model_json_schema(), ensure_ascii=False))
                assert (
                    estimate_tokens(prompt) + schema_size + 1024 + self.metadata["max_output_tokens"] <= 16000
                )
                self.calls.append(stage)
                data = json.loads(prompt.split("INPUT_DATA_JSON:\n", 1)[1])
                self.sent_ids = {p["passage_id"] for p in data["passages"]}
                passage = data["passages"][-1]
                return Extraction(
                    claims=[
                        ClaimProposal(
                            text="版2はLinuxでのみ動作する．",
                            evidence=[
                                EvidenceProposal(
                                    source_id=passage["source_id"],
                                    passage_id=passage["passage_id"],
                                    quote=passage["text"][:100],
                                    relation="supports",
                                )
                            ],
                        )
                    ]
                )
            return await super().run(output_type, prompt, stage=stage)

    model = ContextCheckingModel()
    w = worker(tmp_path, model=model, fetcher=JapaneseFetcher())
    result = await w.run("version 2 の制約")
    assert result.state == "completed"
    all_ids = {p.passage_id for s in result.sources for p in s.passages}
    read_ids = {passage_id for s in result.sources for passage_id in s.read_passage_ids}
    assert model.sent_ids == read_ids == w.extraction_passage_ids
    assert 0 < len(read_ids) < len(all_ids)
    assert all(e.passage_id in read_ids for e in result.evidence)
    events = [json.loads(line) for line in (w.store.directory / "events.jsonl").read_text().splitlines()]
    reduction = next(
        event for event in events if event["type"] == "context_reduced" and event["stage"] == "extract"
    )
    assert reduction["retained_count"] == len(read_ids)
    assert reduction["original_count"] == len(all_ids)
