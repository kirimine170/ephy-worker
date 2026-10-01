"""Synthetic link boundaries for reopening a stored job."""

import os
from pathlib import Path

import pytest

from ephy_worker.store import JobStore


def _symlink(link: Path, target: Path, *, directory: bool) -> None:
    try:
        link.symlink_to(target, target_is_directory=directory)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlink creation unavailable in this test environment: {exc}")


def test_open_existing_rejects_linked_job_directory_before_reading_target(tmp_path):
    root = tmp_path / "jobs"
    root.mkdir()
    outside = tmp_path / "synthetic-git-target"
    outside.mkdir()
    (outside / ".git").mkdir()
    linked = root / "research-linked"
    _symlink(linked, outside, directory=True)

    with pytest.raises(ValueError, match="unsafe job path"):
        JobStore.open_existing(root, linked.name)

    assert not (outside / "events.jsonl").exists()


def test_open_existing_rejects_linked_event_file_before_appending(tmp_path):
    root = tmp_path / "jobs"
    store = JobStore(root)
    outside = tmp_path / "synthetic-events.jsonl"
    outside.write_text("unchanged\n", encoding="utf-8")
    _symlink(store.directory / "events.jsonl", outside, directory=False)

    with pytest.raises(ValueError, match="unsafe job path"):
        JobStore.open_existing(root, store.job_id)

    assert outside.read_text(encoding="utf-8") == "unchanged\n"


def test_open_existing_rejects_junction_classifier(tmp_path, monkeypatch):
    root = tmp_path / "jobs"
    store = JobStore(root)
    original = Path.is_junction
    monkeypatch.setattr(
        Path,
        "is_junction",
        lambda path: path == store.directory or original(path),
    )

    with pytest.raises(ValueError, match="unsafe job path"):
        JobStore.open_existing(root, store.job_id)


def test_job_directory_git_marker_blocks_reopen_and_event(tmp_path):
    store = JobStore(tmp_path / "jobs")
    (store.directory / ".git").mkdir()

    with pytest.raises(ValueError, match="Git checkout"):
        JobStore.open_existing(store.directory.parent, store.job_id)
    with pytest.raises(ValueError, match="Git checkout"):
        store.event("synthetic")
    assert not (store.directory / "events.jsonl").exists()


def test_hardlinked_event_artifact_blocks_append(tmp_path):
    store = JobStore(tmp_path / "jobs")
    outside = tmp_path / "outside-events.jsonl"
    outside.write_bytes(b"unchanged\n")
    try:
        os.link(outside, store.directory / "events.jsonl")
    except OSError as exc:
        pytest.fail(f"hard-link fixture unavailable: {exc}")

    with pytest.raises(ValueError, match="unsafe job path"):
        store.event("synthetic")
    assert outside.read_bytes() == b"unchanged\n"
