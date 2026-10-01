"""Independent acceptance checks for the experimental resume candidate."""

from argparse import Namespace
from types import SimpleNamespace

import pytest
from test_research import FakeFetcher, FakeModel, FakeSearch, worker

from ephy_worker import cli, config
from ephy_worker.research import ResearchExecutor
from ephy_worker.schema import Limits, Report
from ephy_worker.store import JobStore


@pytest.mark.parametrize(
    ("question", "profile_id", "error"),
    [
        ("different question", "fixture", "question"),
        ("original question", "different-profile", "profile"),
    ],
)
async def test_resume_rejects_mismatched_job_identity_before_model_or_writes(
    tmp_path, question, profile_id, error
):
    model = FakeModel()
    executor = worker(tmp_path, model=model)
    report = Report(
        job_id=executor.store.job_id,
        question="original question",
        profile_id=profile_id,
    )

    with pytest.raises(ValueError, match=error):
        await executor.run(question, existing_report=report)

    assert model.calls == []
    assert not (executor.store.directory / "status.json").exists()
    assert not (executor.store.directory / "events.jsonl").exists()


@pytest.mark.parametrize(
    ("question", "profile"),
    [("different question", "fixture"), ("original question", "other-profile")],
)
async def test_cli_resume_mismatch_returns_explicit_error_before_any_job_write(
    tmp_path, monkeypatch, question, profile
):
    store = JobStore(tmp_path)
    store.save(Report(job_id=store.job_id, question="original question", profile_id="fixture"))
    config_fixture = SimpleNamespace(
        limits=Limits(),
        model_profiles={
            "fixture": SimpleNamespace(resolve=lambda: None),
            "other-profile": SimpleNamespace(resolve=lambda: None),
        },
        search=SimpleNamespace(endpoint=lambda: None, resolve_api_key=lambda: None),
    )
    monkeypatch.setattr(config, "load_config", lambda _: config_fixture)
    args = Namespace(
        config=tmp_path / "unused.yaml",
        command="research",
        profile=profile,
        question=question,
        output_dir=tmp_path,
        source_url=[],
        mode="local",
        resume_job_id=store.job_id,
    )

    with pytest.raises(cli.CLIError) as caught:
        await cli.dispatch(args)

    assert caught.value.code == "resume_job_invalid"
    assert not (store.directory / "events.jsonl").exists()
    assert len([path for path in tmp_path.iterdir() if path.is_dir()]) == 1


async def test_matching_resume_is_explicitly_unavailable_before_provider_setup(tmp_path, monkeypatch):
    store = JobStore(tmp_path)
    store.save(Report(job_id=store.job_id, question="original question", profile_id="fixture"))
    events = store.directory / "events.jsonl"
    config_fixture = SimpleNamespace(
        limits=Limits(),
        model_profiles={"fixture": SimpleNamespace(resolve=lambda: pytest.fail("provider setup ran"))},
        search=SimpleNamespace(
            endpoint=lambda: pytest.fail("search setup ran"),
            resolve_api_key=lambda: pytest.fail("credential lookup ran"),
        ),
    )
    monkeypatch.setattr(config, "load_config", lambda _: config_fixture)
    args = Namespace(
        config=tmp_path / "unused.yaml",
        command="research",
        profile="fixture",
        question="original question",
        output_dir=tmp_path,
        source_url=[],
        mode="local",
        resume_job_id=store.job_id,
    )

    with pytest.raises(cli.CLIError) as caught:
        await cli.dispatch(args)

    assert caught.value.code == "resume_checkpoint_unsupported"
    assert not events.exists()


async def test_explicit_empty_resume_id_is_invalid_before_provider_setup(tmp_path, monkeypatch):
    config_fixture = SimpleNamespace(
        limits=Limits(),
        model_profiles={"fixture": SimpleNamespace(resolve=lambda: pytest.fail("provider setup ran"))},
        search=SimpleNamespace(
            endpoint=lambda: pytest.fail("search setup ran"),
            resolve_api_key=lambda: pytest.fail("credential lookup ran"),
        ),
    )
    monkeypatch.setattr(config, "load_config", lambda _: config_fixture)
    args = cli.parser().parse_args(
        [
            "research",
            "--config", str(tmp_path / "unused.yaml"),
            "--profile", "fixture",
            "--question", "original question",
            "--output-dir", str(tmp_path),
            "--resume-job-id", "",
        ]
    )

    with pytest.raises(cli.CLIError) as caught:
        await cli.dispatch(args)

    assert caught.value.code == "resume_job_invalid"
    assert not list(tmp_path.iterdir())


async def test_reopened_store_cannot_enter_new_job_path(tmp_path):
    original = worker(tmp_path)
    report = Report(job_id=original.store.job_id, question="original question", profile_id="fixture")
    original.store.save(report)
    before_status = (original.store.directory / "status.json").read_bytes()
    reopened = JobStore.open_existing(tmp_path, original.store.job_id)
    model = FakeModel()
    executor = ResearchExecutor(
        original.config,
        "fixture",
        reopened,
        model=model,
        search=FakeSearch(),
        fetcher=FakeFetcher(),
    )

    with pytest.raises(ValueError, match="existing_store_requires_checkpoint"):
        await executor.run(report.question)

    assert model.calls == []
    assert (original.store.directory / "status.json").read_bytes() == before_status
    assert not (original.store.directory / "events.jsonl").exists()
