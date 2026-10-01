"""Fixed offline acceptance for JobStore Git and hard-link boundaries.

This fixture lives outside the candidate checkout and never calls a model,
network service, or external agent. Keep its assertions fixed across candidate
revisions.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest
from ephy_worker.schema import Report
from ephy_worker.store import JobStore


def _entrypoints(store: JobStore) -> None:
    report = Report(job_id=store.job_id, question="synthetic", profile_id="fixture")
    with pytest.raises(ValueError, match="(Git checkout|unsafe job path)"):
        JobStore.open_existing(store.directory.parent, store.job_id)
    with pytest.raises(ValueError, match="(Git checkout|unsafe job path)"):
        store.event("synthetic")
    with pytest.raises(ValueError, match="(Git checkout|unsafe job path)"):
        store.status(report)
    with pytest.raises(ValueError, match="(Git checkout|unsafe job path)"):
        store.save(report)
    with pytest.raises(ValueError, match="(Git checkout|unsafe job path)"):
        store.load_report()


@pytest.mark.parametrize("git_marker_kind", ["directory", "file"])
def test_git_marker_inside_normal_job_directory_blocks_every_entrypoint(tmp_path, git_marker_kind):
    store = JobStore(tmp_path / "jobs")
    marker = store.directory / ".git"
    if git_marker_kind == "directory":
        marker.mkdir()
    else:
        marker.write_text("gitdir: synthetic\n", encoding="utf-8")

    _entrypoints(store)
    assert not (store.directory / "events.jsonl").exists()
    assert not (store.directory / "status.json").exists()
    assert not (store.directory / "report.json").exists()


@pytest.mark.parametrize(
    "artifact_name",
    ["events.jsonl", "status.json", "report.json", "report.md", "metrics.json", "artifacts.json"],
)
def test_hardlinked_canonical_artifact_blocks_every_entrypoint(tmp_path, artifact_name):
    store = JobStore(tmp_path / "jobs")
    outside = tmp_path / "outside-artifact"
    original = b"outside bytes must remain unchanged\n"
    outside.write_bytes(original)
    try:
        os.link(outside, store.directory / artifact_name)
    except OSError as exc:
        pytest.fail(f"hard-link fixture unavailable: {exc}")
    assert outside.stat().st_nlink >= 2

    _entrypoints(store)
    assert outside.read_bytes() == original
    assert not (store.directory / "events.jsonl").exists() or artifact_name == "events.jsonl"


@pytest.mark.parametrize("link_count", [None, 0, 2])
def test_unknown_or_multiple_link_count_fails_closed(tmp_path, monkeypatch, link_count):
    store = JobStore(tmp_path / "jobs")
    artifact = store.directory / "events.jsonl"
    artifact.write_bytes(b"unchanged\n")
    original_lstat = Path.lstat

    def lstat(path, *args, **kwargs):
        if path == artifact:
            return SimpleNamespace(st_mode=stat.S_IFREG, st_nlink=link_count)
        return original_lstat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", lstat)
    with pytest.raises(ValueError, match="unsafe job path"):
        store.event("synthetic")
    assert artifact.read_bytes() == b"unchanged\n"
