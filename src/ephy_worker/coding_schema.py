"""Typed contracts for isolated coding jobs and evaluation results．"""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_validator

from .schema import StrictModel

FailureCode = Literal[
    "pi_executable_unavailable",
    "model_unavailable",
    "model_endpoint_unavailable",
    "invalid_model_profile",
    "repository_unavailable",
    "invalid_revision",
    "worktree_creation_failure",
    "pi_rpc_failure",
    "sandbox_unavailable",
    "execution_timeout",
    "cancelled",
    "validation_failure",
    "internal_worker_failure",
]

TaskCategory = Literal[
    "single_file_bug_fix",
    "multi_file_feature",
    "failing_test_repair",
    "behavior_preserving_refactor",
    "repository_navigation",
    "test_failure_driven_fix",
]


def safe_relative_path(value: str) -> str:
    """Accept portable repository-relative paths without traversal．"""

    if not value or "\\" in value or "\x00" in value:
        raise ValueError("path must be a non-empty POSIX repository-relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("path must stay inside the repository")
    return value


class CodingModelProfile(StrictModel):
    """Pi model selection metadata．Credentials remain in Pi/provider configuration．"""

    profile_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{1,79}$")
    provider: str = Field(min_length=1, max_length=80)
    model_id: str = Field(min_length=1, max_length=300)
    server_type: Literal["mock", "pi", "openai-compatible", "llama.cpp"]
    context_window: int = Field(ge=2048, le=2_000_000)
    reasoning_level: Literal["none", "minimal", "low", "medium", "high", "xhigh"] | None = None
    quantization: str | None = Field(default=None, max_length=100)
    endpoint: str | None = Field(default=None, max_length=500)
    pi_executable: str = Field(default="pi", min_length=1, max_length=300)
    pi_args: list[str] = Field(default_factory=list, max_length=32)
    notes: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def mock_is_consistent(self):
        if self.server_type == "mock" and self.provider != "mock":
            raise ValueError("mock server_type requires provider: mock")
        if self.server_type != "mock" and self.provider == "mock":
            raise ValueError("provider: mock requires mock server_type")
        for value in (self.provider, self.model_id, self.pi_executable):
            if "\x00" in value or "\n" in value or "\r" in value:
                raise ValueError("Pi selection fields must be single-line values")
        if any("\x00" in argument or "\n" in argument or "\r" in argument for argument in self.pi_args):
            raise ValueError("pi_args must contain ordinary single-line arguments")
        controlled = {"--mode", "--provider", "--model", "--api-key", "--session", "--session-dir", "--no-session"}
        if any(argument.split("=", 1)[0] in controlled for argument in self.pi_args):
            raise ValueError("pi_args cannot override adapter-owned or credential arguments")
        if self.endpoint is not None:
            try:
                endpoint = urlsplit(self.endpoint)
                if (
                    endpoint.scheme not in {"http", "https"}
                    or not endpoint.hostname
                    or endpoint.username
                    or endpoint.password
                    or endpoint.query
                    or endpoint.fragment
                ):
                    raise ValueError
                _ = endpoint.port
            except ValueError:
                raise ValueError("endpoint must be HTTP(S) without credentials，query or fragment") from None
        return self


class MockFileChange(StrictModel):
    path: str
    content: str = Field(max_length=1_000_000)

    @field_validator("path")
    @classmethod
    def path_is_safe(cls, value: str) -> str:
        return safe_relative_path(value)


class CodingJob(StrictModel):
    """A model-agnostic coding request executed in a detached Git worktree．"""

    job_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{2,127}$")
    task_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{2,127}$")
    goal: str = Field(min_length=1, max_length=20_000)
    repository_path: Path
    base_revision: str = Field(min_length=1, max_length=300)
    source_state: Literal["revision", "working-tree"] = "revision"
    snapshot_untracked_paths: list[str] = Field(default_factory=list, max_length=128)
    validation_command: list[str] = Field(min_length=1, max_length=64)
    timeout_seconds: float = Field(gt=0, le=7200)
    network_policy: Literal["offline", "model-only", "unrestricted"] = "offline"
    macos_sandbox: Literal["off", "write-guard"] = "off"
    macos_protected_roots: list[Path] = Field(default_factory=list, max_length=16)
    model_profile: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{1,79}$")
    mock_changes: list[MockFileChange] = Field(default_factory=list, max_length=128)

    @field_validator("validation_command")
    @classmethod
    def command_is_direct_argv(cls, value: list[str]) -> list[str]:
        if any(not part or "\x00" in part for part in value):
            raise ValueError("validation_command must be a non-empty argv vector")
        return value

    @field_validator("base_revision")
    @classmethod
    def revision_is_an_argument_not_an_option(cls, value: str) -> str:
        if value.startswith("-") or any(character in value for character in ("\x00", "\n", "\r")):
            raise ValueError("base_revision must be a single Git revision argument")
        return value

    @field_validator("snapshot_untracked_paths")
    @classmethod
    def untracked_paths_are_safe(cls, value: list[str]) -> list[str]:
        paths = [safe_relative_path(path) for path in value]
        if len(paths) != len(set(paths)):
            raise ValueError("snapshot_untracked_paths must be unique")
        return paths

    @field_validator("macos_protected_roots")
    @classmethod
    def protected_roots_are_absolute(cls, value: list[Path]) -> list[Path]:
        if any(not path.is_absolute() or str(path) == "/" or "\x00" in str(path) for path in value):
            raise ValueError("macos_protected_roots must contain absolute non-root paths")
        if len(value) != len(set(value)):
            raise ValueError("macos_protected_roots must be unique")
        return value

    @model_validator(mode="after")
    def snapshot_paths_require_working_tree(self):
        if self.source_state == "revision" and self.snapshot_untracked_paths:
            raise ValueError("snapshot_untracked_paths requires source_state: working-tree")
        return self


class CodingFailure(StrictModel):
    code: FailureCode
    stage: Literal["profile", "repository", "worktree", "agent", "validation", "worker"]
    detail: str | None = Field(default=None, max_length=1000)


class AgentMetrics(StrictModel):
    turns: int | None = Field(default=None, ge=0)
    tool_calls: int | None = Field(default=None, ge=0)
    retries: int | None = Field(default=None, ge=0)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)


class AgentOutcome(StrictModel):
    exit_code: int | None = None
    metrics: AgentMetrics = Field(default_factory=AgentMetrics)
    log: str = ""


class ValidationOutcome(StrictModel):
    passed: bool
    exit_code: int | None = None
    log: str = ""


class ModelResult(StrictModel):
    profile: str
    provider: str
    model_id: str
    quantization: str | None = None


class RepositoryResult(StrictModel):
    base_revision: str | None = None
    resulting_revision: str | None = None
    worktree_path: str | None = None
    source_state: Literal["revision", "working-tree"] = "revision"
    source_revision: str | None = None


class ResultStatus(StrictModel):
    status: Literal["succeeded", "failed", "cancelled"]
    validation_passed: bool | None = None
    exit_code: int | None = None
    failure: CodingFailure | None = None


class CodingMetrics(AgentMetrics):
    wall_time: float = Field(ge=0)
    files_changed: int = Field(ge=0)
    diff_additions: int = Field(ge=0)
    diff_deletions: int = Field(ge=0)


class CodingArtifacts(StrictModel):
    patch: str = "patch.diff"
    agent_log: str = "agent.log"
    validation_log: str = "validation.log"
    source_manifest: str | None = None


class CodingResult(StrictModel):
    schema_version: Literal[1] = 1
    run_id: str
    task_id: str
    job_id: str
    model: ModelResult
    repository: RepositoryResult
    result: ResultStatus
    metrics: CodingMetrics
    artifacts: CodingArtifacts = Field(default_factory=CodingArtifacts)
    changed_files: list[str] = Field(default_factory=list)
    worktree_cleaned: bool = False


class CodingPlan(StrictModel):
    job_id: str
    task_id: str
    model: ModelResult
    repository_path: str
    base_revision: str
    worktree: str
    pi_invocation: list[str]
    validation_command: list[str]
    timeout_seconds: float
    network_policy: str
    macos_sandbox: Literal["off", "write-guard"] = "off"
    macos_protected_roots: list[str] = Field(default_factory=list)
    source_state: Literal["revision", "working-tree"] = "revision"
    snapshot_files: int | None = None


class CodingFixture(StrictModel):
    task_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{2,127}$")
    category: TaskCategory
    description: str = Field(min_length=1, max_length=20_000)
    fixture_repository: str = Field(default="generated", min_length=1, max_length=300)
    base_revision: str = "HEAD"
    validation_command: list[str] = Field(min_length=1, max_length=64)
    timeout_seconds: float = Field(default=30, gt=0, le=7200)
    files: dict[str, str] = Field(min_length=1, max_length=128)
    mock_changes: list[MockFileChange] = Field(min_length=1, max_length=128)

    @field_validator("files")
    @classmethod
    def file_paths_are_safe(cls, value: dict[str, str]) -> dict[str, str]:
        for path in value:
            safe_relative_path(path)
        return value


class CodingSuite(StrictModel):
    suite_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{1,79}$")
    tasks: list[CodingFixture] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def task_ids_are_unique(self):
        ids = [task.task_id for task in self.tasks]
        if len(ids) != len(set(ids)):
            raise ValueError("suite task IDs must be unique")
        return self


class CodingProfileCatalog(StrictModel):
    profiles: list[CodingModelProfile] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def profile_ids_are_unique(self):
        ids = [profile.profile_id for profile in self.profiles]
        if len(ids) != len(set(ids)):
            raise ValueError("coding profile IDs must be unique")
        return self

    def by_id(self) -> dict[str, CodingModelProfile]:
        return {profile.profile_id: profile for profile in self.profiles}


def validate_profile_id(value: str) -> str:
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{1,79}", value):
        raise ValueError("invalid coding profile ID")
    return value
