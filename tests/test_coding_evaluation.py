from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from ephy_worker.cli import main
from ephy_worker.coding_profiles import load_coding_profiles
from ephy_worker.evaluation import (
    create_fixture_repository,
    fixture_job,
    load_suite,
    run_evaluation_suite,
)


def test_smoke_suite_represents_all_required_categories():
    suite = load_suite("smoke")

    assert {task.category for task in suite.tasks} == {
        "single_file_bug_fix",
        "multi_file_feature",
        "failing_test_repair",
        "behavior_preserving_refactor",
        "repository_navigation",
        "test_failure_driven_fix",
    }


def test_default_llama_cpp_profile_matches_pi_model_config():
    repository = Path(__file__).resolve().parents[1]
    profiles = load_coding_profiles(repository / "configs" / "coding-models.example.yaml")
    built_in = load_coding_profiles()
    pi_config = json.loads(
        (repository / "configs" / "pi-models.llama-cpp.example.json").read_text(encoding="utf-8")
    )
    provider = pi_config["providers"]["llama_cpp"]
    profile = profiles["qwen3-coder-30b-a3b"]

    assert profile == built_in["qwen3-coder-30b-a3b"]
    assert profile.server_type == "llama.cpp"
    assert profile.endpoint == provider["baseUrl"] == "http://127.0.0.1:8083/v1"
    assert profile.model_id == provider["models"][0]["id"]
    assert provider["models"][0]["contextWindow"] == profile.context_window


def test_router_profiles_match_pi_catalog():
    repository = Path(__file__).resolve().parents[1]
    profiles = load_coding_profiles(repository / "configs" / "coding-models.example.yaml")
    pi_config = json.loads(
        (repository / "configs" / "pi-models.llama-cpp.example.json").read_text(encoding="utf-8")
    )
    assert set(pi_config["providers"]) == {"llama_cpp", "llama_router"}
    provider = pi_config["providers"]["llama_router"]
    models = {model["id"]: model for model in provider["models"]}
    router_profiles = {key: value for key, value in profiles.items() if key.endswith("-router")}

    assert set(models) == {profile.model_id for profile in router_profiles.values()}
    assert provider["baseUrl"] == "http://127.0.0.1:8084/v1"
    for profile in router_profiles.values():
        assert profile.provider == "llama_router"
        assert profile.endpoint == provider["baseUrl"]
        assert profile.context_window == models[profile.model_id]["contextWindow"]
        assert profile.pi_args == ["--offline"]


@pytest.mark.asyncio
async def test_smoke_suite_runs_end_to_end_with_mock(tmp_path):
    suite = load_suite("smoke")
    profile = load_coding_profiles()["mock"]

    runs = await run_evaluation_suite(suite, profile, tmp_path / "runs")

    assert len(runs) == 6
    for result, directory in runs:
        assert result.result.status == "succeeded"
        assert result.result.validation_passed is True
        assert result.metrics.files_changed >= 1
        assert result.worktree_cleaned is True
        assert {path.name for path in directory.iterdir()} == {
            "result.json",
            "patch.diff",
            "agent.log",
            "validation.log",
        }


def test_eval_cli_runs_mock_suite(tmp_path, capsys):
    exit_code = main(
        [
            "eval",
            "run",
            "--suite",
            "smoke",
            "--model",
            "mock",
            "--output-dir",
            str(tmp_path / "runs"),
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert len(payload["runs"]) == 6
    assert all(run["validation_passed"] for run in payload["runs"])


def test_coding_cli_dry_run_resolves_plan_without_artifacts(tmp_path, capsys):
    fixture = load_suite("smoke").tasks[0]
    profile = load_coding_profiles()["mock"]
    repository, revision = create_fixture_repository(fixture)
    try:
        job = fixture_job(fixture, repository, revision, profile, suffix="dry-run")
        job_file = tmp_path / "job.json"
        job_file.write_text(job.model_dump_json(indent=2), encoding="utf-8")

        exit_code = main(["coding", "run", str(job_file), "--dry-run"])

        assert exit_code == 0
        plan = json.loads(capsys.readouterr().out)
        assert plan["base_revision"] == revision
        assert plan["worktree"] == "<worker-owned-temporary-git-worktree>"
        assert not (tmp_path / "runs").exists()
    finally:
        shutil.rmtree(repository, ignore_errors=True)
