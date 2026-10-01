"""Tests for JobStore.open_existing：re-open a recorded job without creating it."""

from __future__ import annotations

import json

import pytest

from ephy_worker.store import JobStore


def test_open_existing_restores_sequence_and_appends(tmp_path):
    store = JobStore(tmp_path)
    store.event("started")
    store.event("round")
    store.event("finished")

    reopened = JobStore.open_existing(tmp_path, store.job_id)
    assert reopened.job_id == store.job_id
    assert reopened.directory == store.directory
    assert reopened.seq == 3

    reopened.event("resumed")
    assert reopened.seq == 4
    lines = (store.directory / "events.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 4
    assert [json.loads(line)["seq"] for line in lines] == [1, 2, 3, 4]
    assert json.loads(lines[-1])["type"] == "resumed"


def test_open_existing_on_empty_directory_starts_at_zero(tmp_path):
    store = JobStore(tmp_path)
    reopened = JobStore.open_existing(tmp_path, store.job_id)
    assert reopened.seq == 0
    reopened.event("first")
    assert reopened.seq == 1


def test_open_existing_does_not_create_missing_directory(tmp_path):
    with pytest.raises(FileNotFoundError):
        JobStore.open_existing(tmp_path, "research-missing")
    assert not (tmp_path / "research-missing").exists()


def test_open_existing_rejects_invalid_job_id(tmp_path):
    with pytest.raises(ValueError):
        JobStore.open_existing(tmp_path, "bad id/")


def test_open_existing_rejects_git_checkout(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "job-1").mkdir()
    with pytest.raises(ValueError):
        JobStore.open_existing(tmp_path, "job-1")


def test_open_existing_rejects_mixed_job_ids(tmp_path):
    store = JobStore(tmp_path)
    events = store.directory / "events.jsonl"
    with events.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps({"job_id": "someone-else", "seq": 1}) + "\n")
    with pytest.raises(ValueError):
        JobStore.open_existing(tmp_path, store.job_id)
