"""Trusted owner-repair controller configuration and stage transitions．"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .formal_artifacts import GateFailure, file_hash, read_json, write_json
from .formal_runtime import (
    command_environment,
    exclusive_lock,
    freeze_submission_identity,
    submit,
    verify_proposal_for_integration,
)
from .owner_repair import OWNER_USER_ID, RepairStore
from .owner_repair_discovery import LocalDiscovery
from .owner_repair_release import DeploymentPending, GitHubRelease, ReviewPending


class RepositoryProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    source: Path
    production: Path
    origin: str
    github_repository: str
    branch: str = "main"
    allowed_prefixes: tuple[str, ...] = ("src/", "tests/", "docs/")
    required_checks: tuple[str, ...] = Field(min_length=1)
    formal_template: Path
    formal_template_sha256: str

    @field_validator("source", "production", "formal_template")
    @classmethod
    def absolute(cls, value):
        if not value.is_absolute() or any(path.is_symlink() or path.is_junction() for path in (value, *value.parents)):
            raise ValueError("registered_path_must_be_absolute_unlinked")
        return value

    @model_validator(mode="after")
    def trusted_repository(self):
        if self.github_repository not in {"kirimine170/ephy-worker", "kirimine170/ephy-runtime"}:
            raise ValueError("repository_not_registered")
        if self.origin != "https://github.com/" + self.github_repository + ".git":
            raise ValueError("registered_origin_mismatch")
        if self.branch != "main" or not re.fullmatch(r"[0-9a-f]{64}", self.formal_template_sha256):
            raise ValueError("invalid_registered_release_identity")
        if not self.allowed_prefixes or any(prefix not in {"src/", "tests/", "docs/", "packages/", "apps/"}
                for prefix in self.allowed_prefixes):
            raise ValueError("invalid_registered_scope")
        return self


class ControllerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    state_dir: Path
    guild_id: int = Field(gt=0)
    channel_ids: tuple[int, ...] = Field(min_length=1)
    repositories: dict[str, RepositoryProfile]
    python: str
    git: str
    gh: str
    powershell: str
    release_adapter: Path
    release_adapter_sha256: str
    review_adapter: Path | None = None
    review_adapter_sha256: str | None = None
    review_adapter_python: Path | None = None
    review_adapter_python_sha256: str | None = None
    review_adapter_config: Path | None = None
    review_adapter_config_sha256: str | None = None
    poll_seconds: float = Field(5, ge=0.1, le=60)

    @model_validator(mode="after")
    def fixed_operator_inputs(self):
        if (not self.repositories or set(self.repositories) - {"ephy-worker", "ephy-runtime"}
                or any(profile.github_repository != "kirimine170/" + alias for alias, profile in self.repositories.items())):
            raise ValueError("invalid_repository_registry")
        for value in (self.state_dir, self.release_adapter, *(Path(value) for value in
                (self.python, self.git, self.gh, self.powershell))):
            if not value.is_absolute() or any(path.is_symlink() or path.is_junction() for path in (value, *value.parents)):
                raise ValueError("operator_paths_must_be_absolute_unlinked")
        if not re.fullmatch(r"[0-9a-f]{64}", self.release_adapter_sha256):
            raise ValueError("release_adapter_pin_required")
        review = (self.review_adapter, self.review_adapter_sha256, self.review_adapter_python,
            self.review_adapter_python_sha256, self.review_adapter_config, self.review_adapter_config_sha256)
        if any(value is not None for value in review):
            if any(value is None for value in review):
                raise ValueError("complete_review_adapter_pins_required")
            for path, pin in zip(review[::2], review[1::2], strict=True):
                if (not path.is_absolute() or any(item.is_symlink() or item.is_junction() for item in (path, *path.parents))
                        or not re.fullmatch(r"[0-9a-f]{64}", pin)):
                    raise ValueError("invalid_review_adapter_identity")
        return self


def load_config(path: Path) -> ControllerConfig:
    if not path.is_absolute() or path.stat().st_size > 262144:
        raise ValueError("owner_controller_config_invalid")
    return ControllerConfig.model_validate(read_json(path))


def formal_child(runtime: dict, pins: dict, arguments: list[str], directory: Path, *, git: str,
                 timeout: int) -> dict:
    """Run one fixed trusted entry point in the operator-selected interpreter．"""
    framework = Path(runtime["governance_root"]).resolve(strict=True)
    module = framework / "src/ephy_worker/owner_repair_pipeline.py"
    python = Path(runtime["python"]).resolve(strict=True)
    if (not Path(runtime["python"]).is_absolute() or not Path(git).is_absolute()
            or pins.get(str(module)) != file_hash(module)
            or pins.get(str(python)) != file_hash(python)):
        raise GateFailure("formal_child_runtime_pin_mismatch")
    if not arguments or arguments[0] not in {"--freeze-draft", "--run-formal", "--verify-formal"}:
        raise GateFailure("formal_child_operation_not_registered")
    temporary = directory / "child-temp"
    temporary.mkdir(parents=True, exist_ok=True)
    environment = command_environment(framework, temporary)
    environment["PYTHONPATH"] = str(framework / "src")
    environment["PATH"] = str(Path(git).parent) + os.pathsep + environment.get("PATH", "")
    argv = [str(python), "-m", "ephy_worker.owner_repair_pipeline", *arguments]
    result = subprocess.run(argv, cwd=framework, env=environment, capture_output=True,
        timeout=timeout, check=False, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    label = arguments[0].removeprefix("--")
    (directory / (label + ".stdout.log")).write_bytes(result.stdout)
    (directory / (label + ".stderr.log")).write_bytes(result.stderr)
    record = {"argv": argv, "cwd": str(framework), "exit_code": result.returncode,
        "python_sha256": file_hash(python), "module_sha256": file_hash(module),
        "environment": environment, "stdout_sha256": file_hash(directory / (label + ".stdout.log")),
        "stderr_sha256": file_hash(directory / (label + ".stderr.log"))}
    write_json(directory / (label + ".execution.json"), record)
    if result.returncode:
        raise GateFailure("trusted_formal_child_failed:" + label)
    return record


def verify_in_interpreter(job_file: Path, directory: Path, *, git: str) -> Path:
    job = read_json(job_file)
    result_file = directory / "integration-verification.json"
    if result_file.exists():
        result_file.unlink()
    formal_child(job["runtime"], job["contract"]["runtime_hashes"],
        ["--verify-formal", str(job_file), "--result", str(result_file)], directory, git=git, timeout=600)
    result = read_json(result_file)
    patch = Path(result["patch"])
    if (result.get("verified") is not True or result.get("job_sha256") != file_hash(job_file)
            or result.get("patch_sha256") != file_hash(patch)
            or patch != Path(job["jobDir"]) / "candidate.patch"):
        raise GateFailure("formal_child_verification_binding_mismatch")
    return patch


class RepairController:
    def __init__(self, config: ControllerConfig, *, discovery=None, release=None):
        self.config = config
        self.store = RepairStore(config.state_dir)
        self.discovery = discovery or LocalDiscovery(config)
        self.release = release or GitHubRelease(config)

    def tick(self) -> bool:
        job = self.store.next()
        if job is None:
            return False
        if job["data"].get("next_poll_at", 0) > time.time():
            return False
        if (int(job["author"]) != OWNER_USER_ID or int(job["guild"]) != self.config.guild_id
                or int(job["channel"]) not in self.config.channel_ids):
            self.store.transition(job["id"], "failed", error="persisted_authorization_invalid")
            return True
        directory = self.config.state_dir / "execution" / job["id"]
        try:
            if job["stage"] == "queued":
                self.store.transition(job["id"], "discovering")
                prepared = self.discovery.prepare(job, directory)
                job_file = submit(prepared["spec"], self.config.state_dir / "formal")
                self.store.transition(job["id"], "submitted", formal_job=str(job_file), repository=prepared["repository"],
                    frozen_spec_sha256=file_hash(directory / "frozen-spec.json"))
                job = self.store.get(job["id"])
            if job["stage"] in {"discovering", "running"}:
                # A crashed model stage is never silently rerun or given a clean identity．
                raise GateFailure("interrupted_model_stage_retained")
            if job["stage"] == "submitted":
                job_file = Path(job["data"]["formal_job"])
                formal = read_json(job_file)
                self.store.transition(job["id"], "running")
                formal_child(formal["runtime"], formal["contract"]["runtime_hashes"],
                    ["--run-formal", str(job_file)], directory, git=self.config.git,
                    timeout=formal["contract"]["timeout_seconds"] + 120)
                patch = verify_in_interpreter(job_file, directory, git=self.config.git)
                self.store.transition(job["id"], "release_pending", patch_sha256=file_hash(patch))
                job = self.store.get(job["id"])
            if job["stage"] in {"release_pending", "review_pending", "merge_pending", "deployment_pending"}:
                self.release.advance(self.store, job)
            return True
        except ReviewPending:
            self.store.transition(job["id"], "review_pending", waiting_reason="exact_head_ci_and_codex_review",
                next_poll_at=time.time() + 30)
            return True
        except DeploymentPending:
            self.store.transition(job["id"], "deployment_pending", waiting_reason="deployment_result_unconfirmed",
                next_poll_at=time.time() + 60)
            return True
        except Exception as exc:  # noqa: BLE001 — durable controller boundary retains actual failure．
            directory.mkdir(parents=True, exist_ok=True)
            # Avoid returning source/checker exception text or credentials to Discord．
            write_json(directory / "failure.json", {"type": type(exc).__name__, "error": str(exc)[:8192],
                "stage": self.store.get(job["id"])["stage"]})
            self.store.finish(job["id"], "failed", "修正を進めましたが，検証または反映の条件を満たせず停止しました．"
                "完了した変更としては扱っていません．調査と実行記録は保存しています．",
                error_type=type(exc).__name__, failure_sha256=file_hash(directory / "failure.json"))
            return True


def config_example(config: ControllerConfig) -> str:
    """Operator tooling can serialize the validated, fixed configuration．"""
    return json.dumps(config.model_dump(mode="json"), ensure_ascii=False, indent=2)


def main() -> None:
    parser = argparse.ArgumentParser(description="Trusted fixed owner-repair helper")
    operations = parser.add_mutually_exclusive_group(required=True)
    operations.add_argument("--freeze-draft", type=Path)
    operations.add_argument("--run-formal", type=Path)
    operations.add_argument("--verify-formal", type=Path)
    parser.add_argument("--result", type=Path)
    args = parser.parse_args()
    path = args.freeze_draft or args.run_formal or args.verify_formal
    if not path.is_absolute() or (args.result is not None and not args.result.is_absolute()):
        parser.error("Absolute operator paths required")
    document = read_json(path)
    expected_python = Path(document["runtime"]["python"]).resolve(strict=True)
    if expected_python != Path(sys.executable).resolve(strict=True):
        raise GateFailure("formal_child_wrong_interpreter")
    if args.freeze_draft:
        if args.result is None or args.result.exists():
            raise GateFailure("fresh_submission_result_required")
        write_json(args.result, freeze_submission_identity(document))
    elif args.verify_formal:
        if args.result is None or args.result.exists():
            raise GateFailure("fresh_verification_result_required")
        patch = verify_proposal_for_integration(path)
        write_json(args.result, {"verified": True, "job_sha256": file_hash(path),
            "patch": str(patch), "patch_sha256": file_hash(patch)})
    else:
        if args.result is not None:
            parser.error("Run does not accept a result path")
        from .strata_runtime import make_runner
        runner = make_runner(path, initial_job=document)
        with exclusive_lock(Path(runner.runtime["resource_lock"])):
            runner.run()


if __name__ == "__main__":
    main()
