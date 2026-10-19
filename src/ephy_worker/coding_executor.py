"""Pi-backed coding execution inside worker-owned detached Git worktrees．"""

from __future__ import annotations

import asyncio
import ctypes
import hashlib
import json
import os
import platform
import shutil
import signal
import socket
import stat
import subprocess
import tempfile
import time
from collections.abc import Callable, Mapping
from contextlib import suppress
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit
from uuid import uuid4

from .coding_schema import (
    AgentMetrics,
    AgentOutcome,
    CodingFailure,
    CodingJob,
    CodingMetrics,
    CodingModelProfile,
    CodingPlan,
    CodingResult,
    ModelResult,
    RepositoryResult,
    ResultStatus,
    ValidationOutcome,
    safe_relative_path,
)
from .store import atomic_write, output_root

MAX_LOG_BYTES = 2 * 1024**2
MAX_SNAPSHOT_BYTES = 20 * 1024**2
MAX_SNAPSHOT_FILES = 2000


def _macos_write_guard(command: list[str], job: CodingJob, worktree: Path | None = None) -> list[str]:
    """Confine child writes while retaining local model-server connectivity．"""

    if job.macos_sandbox == "off":
        return command
    if platform.system() != "Darwin" or not Path("/usr/bin/sandbox-exec").is_file():
        raise CodingExecutionError("sandbox_unavailable", "profile", "macOS sandbox-exec is unavailable")
    roots = {job.repository_path.expanduser().resolve()}
    roots.update(path.expanduser().resolve() for path in job.macos_protected_roots)
    if worktree is not None:
        resolved_worktree = worktree.resolve()
        if any(resolved_worktree == root or root in resolved_worktree.parents for root in roots):
            raise CodingExecutionError(
                "sandbox_unavailable", "worktree", "temporary worktree is inside a protected root"
            )
    rules = [f"(deny file-write* (subpath {json.dumps(str(root))}))" for root in sorted(roots)]
    profile = "\n".join(["(version 1)", "(allow default)", *rules])
    return ["/usr/bin/sandbox-exec", "-p", profile, *command]


class CodingExecutionError(RuntimeError):
    def __init__(
        self,
        code: str,
        stage: str,
        detail: str | None = None,
        *,
        log: str = "",
        exit_code: int | None = None,
        metrics: AgentMetrics | None = None,
    ):
        self.code = code
        self.stage = stage
        self.detail = detail
        self.log = log
        self.exit_code = exit_code
        self.metrics = metrics
        super().__init__(code)


@dataclass
class PatchSnapshot:
    patch: str = ""
    changed_files: list[str] | None = None
    additions: int = 0
    deletions: int = 0
    resulting_revision: str | None = None

    def __post_init__(self):
        if self.changed_files is None:
            self.changed_files = []


def _git(repository: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            ["git", "-C", str(repository), *args],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CodingExecutionError(
            "repository_unavailable", "repository", type(exc).__name__
        ) from None
    if check and result.returncode:
        detail = (result.stderr or result.stdout).strip()[:1000] or "git command failed"
        raise CodingExecutionError("repository_unavailable", "repository", detail)
    return result


def resolve_repository(repository_path: Path, revision: str) -> tuple[Path, str]:
    requested = repository_path.expanduser().resolve()
    if not requested.is_dir():
        raise CodingExecutionError("repository_unavailable", "repository", "repository is not a directory")
    root_result = _git(requested, "rev-parse", "--show-toplevel", check=False)
    if root_result.returncode:
        raise CodingExecutionError("repository_unavailable", "repository", "path is not a Git worktree")
    root_text = root_result.stdout.strip()
    # MSYS Git reports /c/... even when invoked by native Python. Preserve the
    # user's Git selection and shared gitdir format while comparing native paths.
    if (
        os.name == "nt" and len(root_text) >= 3 and root_text[0] == "/" and root_text[2] == "/"
        and root_text[1].isascii() and root_text[1].isalpha()
    ):
        root_text = f"{root_text[1]}:{root_text[2:]}"
    root = Path(root_text).resolve()
    if root != requested:
        raise CodingExecutionError(
            "repository_unavailable", "repository", "repository_path must name the Git worktree root"
        )
    # ^0 is the commit peel spelling without braces, which MSYS argv globbing
    # can expand before Git sees a native Windows subprocess argument.
    revision_result = _git(root, "rev-parse", "--verify", f"{revision}^0", check=False)
    if revision_result.returncode:
        raise CodingExecutionError("invalid_revision", "repository", "base revision does not resolve")
    return root, revision_result.stdout.strip()


class WorkingTreeSnapshot:
    """Copy selected source files into a disposable Git repository．"""

    def __init__(self, source: Path, source_revision: str, untracked_paths: list[str]):
        self.source = source
        self.source_revision = source_revision
        self.untracked_paths = untracked_paths
        self.temporary_root: Path | None = None
        self._owned_root: Path | None = None
        self.repository: Path | None = None
        self.manifest: dict | None = None

    def files(self) -> list[str]:
        if _git(self.source, "ls-files", "--unmerged").stdout:
            raise CodingExecutionError(
                "worktree_creation_failure", "worktree", "source index has unresolved merges"
            )
        tracked = set(filter(None, _git(self.source, "ls-files", "--cached", "-z").stdout.split("\0")))
        available = set(
            filter(None, _git(self.source, "ls-files", "--others", "--exclude-standard", "-z").stdout.split("\0"))
        )
        extras = set(self.untracked_paths)
        if extras - available:
            raise CodingExecutionError(
                "worktree_creation_failure",
                "worktree",
                "requested untracked path is missing，ignored，or already tracked",
            )
        files = sorted(tracked | extras)
        if not files or len(files) > MAX_SNAPSHOT_FILES:
            raise CodingExecutionError(
                "worktree_creation_failure", "worktree", "snapshot file count is outside allowed range"
            )
        for name in files:
            safe_relative_path(name)
        return files

    def create(self) -> tuple[Path, str, dict]:
        files = self.files()
        self.temporary_root = Path(tempfile.mkdtemp(prefix="ephy-worker-source-"))
        self._owned_root = self.temporary_root.resolve(strict=True)
        self.repository = self.temporary_root / "repository"
        self.repository.mkdir()
        copied: list[dict] = []
        total_bytes = 0
        try:
            for name in files:
                source_file = self.source.joinpath(*name.split("/"))
                current = source_file
                while current != self.source:
                    if current.is_symlink():
                        raise CodingExecutionError(
                            "worktree_creation_failure", "worktree", "snapshot path traverses a symbolic link"
                        )
                    current = current.parent
                try:
                    metadata = source_file.lstat()
                    if not stat.S_ISREG(metadata.st_mode):
                        raise OSError("not a regular file")
                    descriptor = os.open(source_file, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
                    with os.fdopen(descriptor, "rb") as handle:
                        data = handle.read(MAX_SNAPSHOT_BYTES + 1 - total_bytes)
                except OSError:
                    raise CodingExecutionError(
                        "worktree_creation_failure", "worktree", f"cannot read source file: {name}"
                    ) from None
                total_bytes += len(data)
                if total_bytes > MAX_SNAPSHOT_BYTES:
                    raise CodingExecutionError(
                        "worktree_creation_failure", "worktree", "snapshot exceeds 20 MiB"
                    )
                destination = self.repository.joinpath(*name.split("/"))
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(data)
                destination.chmod(0o755 if metadata.st_mode & 0o111 else 0o644)
                copied.append({"path": name, "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)})
            _git(self.repository, "init", "-q", "-b", "snapshot")
            _git(self.repository, "add", "-A")
            _git(
                self.repository,
                "-c", "user.name=Ephy Worker",
                "-c", "user.email=worker@invalid.example",
                "-c", "commit.gpgsign=false",
                "commit", "-q", "-m", "working tree snapshot",
            )
            revision = _git(self.repository, "rev-parse", "HEAD").stdout.strip()
            self.manifest = {
                "source_state": "working-tree",
                "source_revision": self.source_revision,
                "snapshot_revision": revision,
                "files": copied,
            }
            return self.repository, revision, self.manifest
        except Exception:
            self.cleanup()
            raise

    def cleanup(self) -> None:
        if self.temporary_root is not None:
            root = self.temporary_root
            if root.is_symlink() or root.resolve(strict=True) != self._owned_root:
                raise OSError("Snapshot cleanup target is not the allocated directory")

            def remove_readonly_object(function, path, exception):
                target = Path(path)
                metadata = target.lstat()
                # Git marks its own objects read-only on Windows. Clear only that
                # file attribute inside this allocation; ACL failures stay errors.
                if (
                    os.name != "nt"
                    or not isinstance(exception, PermissionError)
                    or function is not os.unlink
                    or not stat.S_ISREG(metadata.st_mode)
                    or metadata.st_nlink != 1
                    or not getattr(metadata, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_READONLY
                    or not target.resolve(strict=True).is_relative_to(self._owned_root)
                ):
                    raise exception
                target.chmod(metadata.st_mode | stat.S_IWRITE)
                function(path)

            shutil.rmtree(root, onexc=remove_readonly_object)
            self.temporary_root = None
            self._owned_root = None
            self.repository = None


class GitWorktree:
    """One worker-owned worktree．Cleanup never resets the source checkout．"""

    def __init__(self, repository: Path, base_revision: str):
        self.repository = repository
        self.base_revision = base_revision
        self.temporary_root: Path | None = None
        self.path: Path | None = None

    def create(self) -> Path:
        self.temporary_root = Path(tempfile.mkdtemp(prefix="ephy-worker-coding-"))
        self.path = self.temporary_root / "worktree"
        result = _git(
            self.repository,
            "worktree",
            "add",
            "--detach",
            str(self.path),
            self.base_revision,
            check=False,
        )
        if result.returncode:
            detail = (result.stderr or result.stdout).strip()[:1000]
            shutil.rmtree(self.temporary_root, ignore_errors=True)
            self.path = None
            self.temporary_root = None
            raise CodingExecutionError("worktree_creation_failure", "worktree", detail)
        return self.path

    def snapshot(self) -> PatchSnapshot:
        if self.path is None:
            return PatchSnapshot()
        untracked_raw = _git(
            self.path, "ls-files", "--others", "--exclude-standard", "-z"
        ).stdout
        untracked = [entry for entry in untracked_raw.split("\0") if entry]
        if untracked:
            # Intent-to-add only touches this worktree's private index and makes new files
            # visible in the candidate patch without staging their content．
            _git(self.path, "add", "-N", "--", *untracked)
        patch = _git(
            self.path, "diff", "--binary", "--no-ext-diff", self.base_revision, "--"
        ).stdout
        changed = [
            entry
            for entry in _git(
                self.path, "diff", "--name-only", "-z", self.base_revision, "--"
            ).stdout.split("\0")
            if entry
        ]
        additions = deletions = 0
        for line in _git(
            self.path, "diff", "--numstat", self.base_revision, "--"
        ).stdout.splitlines():
            parts = line.split("\t", 2)
            if len(parts) >= 2:
                if parts[0].isdigit():
                    additions += int(parts[0])
                if parts[1].isdigit():
                    deletions += int(parts[1])
        resulting = _git(self.path, "rev-parse", "HEAD").stdout.strip()
        return PatchSnapshot(patch, changed, additions, deletions, resulting)

    def cleanup(self) -> bool:
        if self.path is None or self.temporary_root is None:
            return True
        result = _git(
            self.repository, "worktree", "remove", "--force", str(self.path), check=False
        )
        if result.returncode:
            # Preserve the worker-owned directory for diagnosis when Git refuses removal．
            return False
        shutil.rmtree(self.temporary_root, ignore_errors=True)
        return not self.temporary_root.exists()


class PiRunner(Protocol):
    def invocation(self, profile: CodingModelProfile) -> list[str]: ...

    async def run(
        self,
        job: CodingJob,
        profile: CodingModelProfile,
        worktree: Path,
        cancel_event: asyncio.Event,
    ) -> AgentOutcome: ...


class FakePiRunner:
    """Deterministic no-model runner used by fixtures and offline tests．"""

    def __init__(self, *, delay_seconds: float = 0):
        self.delay_seconds = delay_seconds

    def invocation(self, profile: CodingModelProfile) -> list[str]:
        return ["mock-pi", "--model", profile.model_id]

    async def run(
        self,
        job: CodingJob,
        profile: CodingModelProfile,
        worktree: Path,
        cancel_event: asyncio.Event,
    ) -> AgentOutcome:
        if self.delay_seconds:
            await asyncio.sleep(self.delay_seconds)
        if cancel_event.is_set():
            raise CodingExecutionError("cancelled", "agent", "cancel requested")
        if not job.mock_changes:
            raise CodingExecutionError(
                "invalid_model_profile", "profile", "mock profile requires deterministic mock_changes"
            )
        changed: list[str] = []
        root = worktree.resolve()
        for change in job.mock_changes:
            if cancel_event.is_set():
                raise CodingExecutionError("cancelled", "agent", "cancel requested")
            target = root.joinpath(*change.path.split("/"))
            try:
                target.relative_to(root)
            except ValueError:
                raise CodingExecutionError("internal_worker_failure", "worker", "mock path escaped") from None
            current = target
            while current != root:
                if current.is_symlink():
                    raise CodingExecutionError(
                        "internal_worker_failure", "worker", "mock change traverses a symbolic link"
                    )
                current = current.parent
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(change.content, encoding="utf-8", newline="\n")
            changed.append(change.path)
        log = json.dumps(
            {"type": "mock_agent_end", "model": profile.model_id, "changed_files": changed},
            ensure_ascii=False,
        ) + "\n"
        return AgentOutcome(
            exit_code=0,
            metrics=AgentMetrics(turns=1, tool_calls=len(changed), retries=0, input_tokens=None, output_tokens=None),
            log=log,
        )


def _pi_model_argument(profile: CodingModelProfile) -> str:
    if profile.reasoning_level and profile.reasoning_level != "none":
        return f"{profile.model_id}:{profile.reasoning_level}"
    return profile.model_id


def _sanitize_event(value):
    """Remove model reasoning and likely credentials before persisting RPC events．"""

    if isinstance(value, list):
        return [_sanitize_event(item) for item in value]
    if not isinstance(value, dict):
        return value
    event_type = str(value.get("type", ""))
    if event_type.startswith("thinking"):
        return {"type": event_type, "redacted": True}
    sanitized = {}
    for key, item in value.items():
        lowered = key.lower().replace("_", "")
        if lowered in {"apikey", "authorization", "credential", "secret"} or lowered in {"thinking", "reasoning"}:
            sanitized[key] = "[redacted]"
        else:
            sanitized[key] = _sanitize_event(item)
    return sanitized


def _agent_error(event: dict) -> str | None:
    """Extract provider/model failures without treating ordinary tool failures as fatal．"""

    event_type = event.get("type")
    if event_type == "extension_error":
        return str(event.get("error") or event.get("message") or "Pi extension failed")
    messages = []
    if event_type == "message_end" and isinstance(event.get("message"), dict):
        messages = [event["message"]]
    elif event_type == "agent_end" and isinstance(event.get("messages"), list):
        if event.get("willRetry"):
            return None
        messages = [
            message
            for message in event["messages"]
            if isinstance(message, dict) and message.get("role") == "assistant"
        ][-1:]
    for message in messages:
        stop_reason = message.get("stopReason", message.get("stop_reason"))
        if stop_reason == "aborted":
            return "Pi model stream aborted before completion"
        if stop_reason == "error":
            return str(
                message.get("errorMessage")
                or message.get("error_message")
                or message.get("error")
                or "Pi model request failed"
            )
    return None


class _WindowsJob:
    """One non-inheritable job handle for a suspended, worker-owned Pi process tree."""

    CREATE_SUSPENDED = 0x00000004

    def __init__(self):
        if os.name != "nt":
            raise OSError("Windows jobs are only available on Windows")
        self.handle = None
        self.api = ctypes.WinDLL("kernel32", use_last_error=True)

        class BasicLimits(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong), ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class IoCounters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
            )]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BasicLimits), ("IoInfo", IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        class Accounting(ctypes.Structure):
            _fields_ = [
                ("TotalUserTime", ctypes.c_longlong), ("TotalKernelTime", ctypes.c_longlong),
                ("ThisPeriodTotalUserTime", ctypes.c_longlong), ("ThisPeriodTotalKernelTime", ctypes.c_longlong),
                ("TotalPageFaultCount", wintypes.DWORD), ("TotalProcesses", wintypes.DWORD),
                ("ActiveProcesses", wintypes.DWORD), ("TotalTerminatedProcesses", wintypes.DWORD),
            ]

        class ThreadEntry(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ThreadID", wintypes.DWORD),
                ("th32OwnerProcessID", wintypes.DWORD), ("tpBasePri", wintypes.LONG),
                ("tpDeltaPri", wintypes.LONG), ("dwFlags", wintypes.DWORD),
            ]

        self.Accounting = Accounting
        self.ThreadEntry = ThreadEntry
        signatures = {
            "CreateJobObjectW": ([ctypes.c_void_p, wintypes.LPCWSTR], wintypes.HANDLE),
            "SetInformationJobObject": (
                [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD], wintypes.BOOL,
            ),
            "AssignProcessToJobObject": ([wintypes.HANDLE, wintypes.HANDLE], wintypes.BOOL),
            "TerminateJobObject": ([wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
            "QueryInformationJobObject": (
                [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p], wintypes.BOOL,
            ),
            "OpenProcess": ([wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
            "IsProcessInJob": ([wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)], wintypes.BOOL),
            "WaitForSingleObject": ([wintypes.HANDLE, wintypes.DWORD], wintypes.DWORD),
            "OpenThread": ([wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
            "GetProcessIdOfThread": ([wintypes.HANDLE], wintypes.DWORD),
            "ResumeThread": ([wintypes.HANDLE], wintypes.DWORD),
            "CreateToolhelp32Snapshot": ([wintypes.DWORD, wintypes.DWORD], wintypes.HANDLE),
            "Thread32First": ([wintypes.HANDLE, ctypes.POINTER(ThreadEntry)], wintypes.BOOL),
            "Thread32Next": ([wintypes.HANDLE, ctypes.POINTER(ThreadEntry)], wintypes.BOOL),
            "CloseHandle": ([wintypes.HANDLE], wintypes.BOOL),
        }
        for name, (arguments, result) in signatures.items():
            function = getattr(self.api, name)
            function.argtypes = arguments
            function.restype = result
        # NULL security attributes make this handle non-inheritable. Children
        # cannot retain it and defeat KILL_ON_JOB_CLOSE when the GUI exits.
        self.handle = self.api.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = ExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = 0x00002000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.api.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            error = ctypes.get_last_error()
            self.close()
            raise ctypes.WinError(error)

    def assign_and_resume(self, pid: int) -> None:
        # PROCESS_TERMINATE | PROCESS_SET_QUOTA, required by assignment.
        process_handle = self.api.OpenProcess(0x0001 | 0x0100, False, pid)
        if not process_handle:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            if not self.api.AssignProcessToJobObject(self.handle, process_handle):
                raise ctypes.WinError(ctypes.get_last_error())
            self._resume_primary_thread(pid)
        finally:
            self.api.CloseHandle(process_handle)

    def _resume_primary_thread(self, pid: int) -> None:
        # Popen closes CreateProcess's primary-thread handle. Recover only the
        # one thread in our still-suspended process via documented ToolHelp APIs.
        snapshot = self.api.CreateToolhelp32Snapshot(0x00000004, 0)  # TH32CS_SNAPTHREAD
        if snapshot == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            entry = self.ThreadEntry()
            entry.dwSize = ctypes.sizeof(entry)
            available = self.api.Thread32First(snapshot, ctypes.byref(entry))
            thread_ids = []
            while available:
                if entry.th32OwnerProcessID == pid:
                    thread_ids.append(entry.th32ThreadID)
                entry.dwSize = ctypes.sizeof(entry)
                available = self.api.Thread32Next(snapshot, ctypes.byref(entry))
            if ctypes.get_last_error() != 18:  # ERROR_NO_MORE_FILES
                raise ctypes.WinError(ctypes.get_last_error())
            if len(thread_ids) != 1:
                raise OSError("suspended Pi process must have exactly one primary thread")
            # THREAD_SUSPEND_RESUME | THREAD_QUERY_LIMITED_INFORMATION.
            thread = self.api.OpenThread(0x0002 | 0x0800, False, thread_ids[0])
            if not thread:
                raise ctypes.WinError(ctypes.get_last_error())
            try:
                if self.api.GetProcessIdOfThread(thread) != pid:
                    raise OSError("primary thread ownership changed")
                if self.api.ResumeThread(thread) != 1:
                    raise OSError("primary thread was not exclusively suspended by the worker")
            finally:
                self.api.CloseHandle(thread)
        finally:
            self.api.CloseHandle(snapshot)

    def terminate(self) -> None:
        if not self.api.TerminateJobObject(self.handle, 1):
            raise ctypes.WinError(ctypes.get_last_error())

    def process_handles(self) -> list[int]:
        """Retain exit handles for current members, rejecting reused/nonmember PIDs."""
        capacity = 64
        while capacity <= 8192:
            class ProcessIds(ctypes.Structure):
                _fields_ = [
                    ("NumberOfAssignedProcesses", wintypes.DWORD),
                    ("NumberOfProcessIdsInList", wintypes.DWORD),
                    ("ProcessIdList", ctypes.c_size_t * capacity),
                ]

            entries = ProcessIds()
            if self.api.QueryInformationJobObject(
                self.handle, 3, ctypes.byref(entries), ctypes.sizeof(entries), None,
            ):
                break
            if ctypes.get_last_error() != 234:  # ERROR_MORE_DATA
                raise ctypes.WinError(ctypes.get_last_error())
            capacity *= 2
        else:
            raise OSError("owned process tree exceeds the bounded exit-handle capacity")
        handles = []
        try:
            for pid in entries.ProcessIdList[:entries.NumberOfProcessIdsInList]:
                # SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION.
                handle = self.api.OpenProcess(0x00100000 | 0x1000, False, pid)
                if not handle:
                    if ctypes.get_last_error() == 87:  # Process already exited.
                        continue
                    raise ctypes.WinError(ctypes.get_last_error())
                try:
                    belongs = wintypes.BOOL()
                    if not self.api.IsProcessInJob(handle, self.handle, ctypes.byref(belongs)):
                        raise ctypes.WinError(ctypes.get_last_error())
                    if not belongs.value:
                        raise OSError("owned process identity changed during cleanup")
                except BaseException:
                    self.api.CloseHandle(handle)
                    raise
                handles.append(handle)
            return handles
        except BaseException:
            self.close_process_handles(handles)
            raise

    def processes_exited(self, handles: list[int]) -> bool:
        complete = True
        for handle in handles:
            status = self.api.WaitForSingleObject(handle, 0)
            if status == 258:  # WAIT_TIMEOUT
                complete = False
            elif status != 0:  # WAIT_OBJECT_0
                raise ctypes.WinError(ctypes.get_last_error())
        return complete

    def close_process_handles(self, handles: list[int]) -> None:
        for handle in handles:
            self.api.CloseHandle(handle)

    def active_processes(self) -> int:
        accounting = self.Accounting()
        if not self.api.QueryInformationJobObject(
            self.handle, 1, ctypes.byref(accounting), ctypes.sizeof(accounting), None,
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        return int(accounting.ActiveProcesses)

    def close(self) -> None:
        if self.handle is not None:
            if not self.api.CloseHandle(self.handle):
                raise ctypes.WinError(ctypes.get_last_error())
            self.handle = None


async def _stop_process(process: asyncio.subprocess.Process, windows_job: _WindowsJob | None = None) -> None:
    if windows_job is not None:
        cleanup_error = None
        handles = []
        try:
            handles = windows_job.process_handles()
        except OSError as exc:
            cleanup_error = exc
        try:
            windows_job.terminate()
            deadline = time.monotonic() + 3
            while windows_job.active_processes() or not windows_job.processes_exited(handles):
                if time.monotonic() >= deadline:
                    raise OSError("owned Windows process tree did not stop")
                await asyncio.sleep(0.02)
        except OSError as exc:
            cleanup_error = exc
        finally:
            windows_job.close_process_handles(handles)
            try:
                windows_job.close()
            except OSError as exc:
                cleanup_error = cleanup_error or exc
        # A failed assignment leaves the newly-created process suspended and
        # outside the job. Stop that exact Popen handle too; it cannot have children.
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            except OSError as exc:
                cleanup_error = cleanup_error or exc
        try:
            await asyncio.wait_for(process.wait(), timeout=2)
        except TimeoutError:
            raise CodingExecutionError(
                "internal_worker_failure", "worker", "Pi process exit could not be confirmed",
            ) from None
        if cleanup_error is not None:
            raise CodingExecutionError(
                "internal_worker_failure", "worker", "Windows Pi process-tree cleanup could not be confirmed",
            ) from cleanup_error
        return
    if os.name == "posix":
        # start_new_session=True makes the child's PID its process-group ID．
        # The leader can exit while a tool subprocess remains in that group．
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
        if process.returncode is None:
            try:
                await asyncio.wait_for(process.wait(), timeout=2)
            except TimeoutError:
                pass
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            try:
                os.killpg(process.pid, 0)
            except ProcessLookupError:
                break
            await asyncio.sleep(0.05)
        else:
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
        if process.returncode is None:
            await process.wait()
        return
    if process.returncode is not None:
        return
    with suppress(ProcessLookupError):
        process.terminate()
    try:
        await asyncio.wait_for(process.wait(), timeout=2)
    except TimeoutError:
        with suppress(ProcessLookupError):
            process.kill()
        try:
            await asyncio.wait_for(process.wait(), timeout=2)
        except TimeoutError:
            raise CodingExecutionError(
                "internal_worker_failure", "worker", "process exit could not be confirmed",
            ) from None


async def _watch_process_exit(process: asyncio.subprocess.Process) -> float:
    # asyncio.Process.wait can itself wait for inherited stdout/stderr handles.
    # returncode is populated when the actual leader exits, independently of EOF.
    while process.returncode is None:
        await asyncio.sleep(0.02)
    return time.monotonic()


async def _read_rpc_line(process: asyncio.subprocess.Process, exited: asyncio.Task) -> bytes:
    assert process.stdout is not None
    if exited.done() and time.monotonic() - exited.result() >= 0.25:
        return b""
    read = asyncio.create_task(process.stdout.readline(), name="pi-stdout-line")
    try:
        done, _ = await asyncio.wait((read, exited), return_when=asyncio.FIRST_COMPLETED)
        if read in done:
            return await read
        # Permit final buffered bytes after leader exit, but never let an orphan
        # tool hold this client open merely by inheriting stdout.
        remaining = max(0.001, 0.25 - (time.monotonic() - exited.result()))
        try:
            return await asyncio.wait_for(read, timeout=remaining)
        except TimeoutError:
            return b""
    finally:
        if not read.done():
            read.cancel()
            with suppress(asyncio.CancelledError):
                await read


async def _finish_stderr(task: asyncio.Task[str]) -> str:
    try:
        return await asyncio.wait_for(task, timeout=2)
    except TimeoutError:
        raise CodingExecutionError("pi_rpc_failure", "agent", "Pi stderr did not close after shutdown") from None


async def _read_capped(stream: asyncio.StreamReader | None, limit: int = MAX_LOG_BYTES) -> str:
    if stream is None:
        return ""
    chunks: list[bytes] = []
    size = 0
    while True:
        chunk = await stream.read(65536)
        if not chunk:
            break
        if size < limit:
            kept = chunk[: limit - size]
            chunks.append(kept)
            size += len(kept)
    text = b"".join(chunks).decode("utf-8", errors="replace")
    if size >= limit:
        text += "\n[log truncated]\n"
    return text


async def _check_endpoint(profile: CodingModelProfile) -> None:
    if not profile.endpoint:
        return
    parsed = urlsplit(profile.endpoint)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise CodingExecutionError(
            "invalid_model_profile", "profile", "endpoint must be an HTTP(S) URL"
        )
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(parsed.hostname, port, family=socket.AF_UNSPEC), timeout=3
        )
        writer.close()
        await writer.wait_closed()
    except (OSError, TimeoutError):
        raise CodingExecutionError(
            "model_endpoint_unavailable", "profile", "configured model endpoint is unreachable"
        ) from None


class PiRpcRunner:
    """Strict JSONL client for ``pi --mode rpc``．"""

    def __init__(
        self,
        *,
        observer: Callable[[dict], None] | None = None,
        child_environment: Mapping[str, str] | None = None,
        process_observer: Callable[[int | None], None] | None = None,
        required_ready_event: str | None = None,
    ):
        self.observer = observer
        self.child_environment = dict(child_environment or {})
        self.process_observer = process_observer
        self.required_ready_event = required_ready_event

    def invocation(self, profile: CodingModelProfile) -> list[str]:
        return [
            profile.pi_executable,
            "--mode",
            "rpc",
            "--no-session",
            "--provider",
            profile.provider,
            "--model",
            _pi_model_argument(profile),
            *profile.pi_args,
        ]

    async def run(
        self,
        job: CodingJob,
        profile: CodingModelProfile,
        worktree: Path,
        cancel_event: asyncio.Event,
    ) -> AgentOutcome:
        executable = profile.pi_executable
        resolved = executable if Path(executable).is_absolute() else shutil.which(executable)
        if not resolved or not Path(resolved).is_file():
            raise CodingExecutionError("pi_executable_unavailable", "agent", "Pi executable was not found")
        await _check_endpoint(profile)
        command = _macos_write_guard(self.invocation(profile), job, worktree)
        environment = os.environ.copy()
        environment.update(getattr(self, "child_environment", {}))
        environment["EPHY_NETWORK_POLICY"] = job.network_policy
        windows_job = None
        if os.name == "nt":
            try:
                windows_job = _WindowsJob()
            except OSError:
                raise CodingExecutionError(
                    "sandbox_unavailable", "agent", "Windows Pi process-tree containment is unavailable",
                ) from None
        kwargs = (
            {"start_new_session": True} if os.name == "posix"
            else {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0) | _WindowsJob.CREATE_SUSPENDED}
        )
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=worktree,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=environment,
                limit=1024 * 1024,
                **kwargs,
            )
        except OSError:
            if windows_job is not None:
                windows_job.close()
            code = "sandbox_unavailable" if job.macos_sandbox != "off" else "pi_executable_unavailable"
            raise CodingExecutionError(code, "agent", "Pi sandbox could not be started") from None
        except BaseException:
            if windows_job is not None:
                windows_job.close()
            raise

        prompt = (
            "Work only inside the current Git worktree. Do not commit, push, merge, download models, "
            "or modify files outside this worktree. Do not modify existing tests unless the task "
            "explicitly asks for test changes. When running Python commands, use python3 rather than python. "
            f"Network policy: {job.network_policy}. Complete this task and then stop:\n\n{job.goal}"
        )
        events: list[str] = []
        errors: list[str] = []
        model_error: str | None = None
        settled = False
        metrics = AgentMetrics(turns=0, tool_calls=0, retries=0)
        stderr_task = asyncio.create_task(_read_capped(process.stderr), name="pi-stderr")
        exited = asyncio.create_task(_watch_process_exit(process), name="pi-leader-exit")
        try:
            process_observer = getattr(self, "process_observer", None)
            if process_observer is not None:
                process_observer(process.pid)
            if windows_job is not None:
                try:
                    windows_job.assign_and_resume(process.pid)
                except OSError:
                    raise CodingExecutionError(
                        "sandbox_unavailable", "agent", "Windows Pi process-tree containment could not start",
                    ) from None
            ready_event = getattr(self, "required_ready_event", None)
            if ready_event is not None:
                assert process.stdout is not None
                deadline = time.monotonic() + 10
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise CodingExecutionError("pi_rpc_failure", "agent", "required Pi guard did not start")
                    try:
                        line = await asyncio.wait_for(_read_rpc_line(process, exited), timeout=remaining)
                        bootstrap = json.loads(line.decode("utf-8"))
                    except (TimeoutError, UnicodeDecodeError, json.JSONDecodeError):
                        raise CodingExecutionError(
                            "pi_rpc_failure", "agent", "required Pi guard did not start"
                        ) from None
                    if not isinstance(bootstrap, dict) or bootstrap.get("type") in {
                        "extension_error", "ephy_navigation_guard_failed", "ephy_navigation_guard_violation",
                    }:
                        raise CodingExecutionError("pi_rpc_failure", "agent", "required Pi guard failed")
                    sanitized = _sanitize_event(bootstrap)
                    events.append(json.dumps(sanitized, ensure_ascii=False, separators=(",", ":")))
                    observer = getattr(self, "observer", None)
                    if observer is not None:
                        observer(sanitized)
                    if bootstrap.get("type") == ready_event and bootstrap.get("version") == 1:
                        break
            assert process.stdin is not None
            process.stdin.write(
                (json.dumps({"id": "coding-job", "type": "prompt", "message": prompt}) + "\n").encode("utf-8")
            )
            await process.stdin.drain()
            assert process.stdout is not None
            while True:
                line = await _read_rpc_line(process, exited)
                if not line:
                    break
                try:
                    event = json.loads(line.decode("utf-8", errors="strict").rstrip("\r\n"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    raise CodingExecutionError(
                        "pi_rpc_failure", "agent", "Pi emitted invalid JSONL"
                    ) from None
                if not isinstance(event, dict):
                    raise CodingExecutionError("pi_rpc_failure", "agent", "Pi emitted a non-object event")
                observer = getattr(self, "observer", None)
                if observer is not None:
                    observer(_sanitize_event(event))
                events.append(json.dumps(_sanitize_event(event), ensure_ascii=False, separators=(",", ":")))
                event_type = event.get("type")
                event_error = _agent_error(event)
                if event_type == "extension_error" and event_error:
                    errors.append(event_error)
                elif event_type == "message_end" and event_error:
                    model_error = event_error
                elif event_type == "agent_end":
                    # Pi may emit an error-ending turn and then retry it．Only the
                    # terminal agent_end decides whether that model error survived．
                    model_error = event_error
                if event_type == "turn_start":
                    metrics.turns = (metrics.turns or 0) + 1
                elif event_type == "tool_execution_start":
                    metrics.tool_calls = (metrics.tool_calls or 0) + 1
                elif event_type == "auto_retry_start":
                    metrics.retries = (metrics.retries or 0) + 1
                elif event_type == "message_update" and isinstance(event.get("usage"), dict):
                    usage = event["usage"]
                    if isinstance(usage.get("input"), int):
                        metrics.input_tokens = usage["input"]
                    if isinstance(usage.get("output"), int):
                        metrics.output_tokens = usage["output"]
                elif event_type == "response" and event.get("command") == "prompt" and not event.get("success"):
                    errors.append(str(event.get("error") or "prompt rejected"))
                    break
                elif event_type == "extension_ui_request" and event.get("method") in {
                    "select", "confirm", "input", "editor"
                }:
                    errors.append("interactive extension request is unsupported")
                    break
                elif event_type == "agent_settled":
                    settled = True
                    break
        finally:
            try:
                await _stop_process(process, windows_job)
            finally:
                try:
                    stderr = await _finish_stderr(stderr_task)
                finally:
                    exited.cancel()
                    with suppress(asyncio.CancelledError):
                        await exited
                    process_observer = getattr(self, "process_observer", None)
                    if process_observer is not None:
                        process_observer(None)
        log = "\n".join(events) + ("\n" if events else "")
        if stderr:
            log += "[stderr]\n" + stderr
        if not settled and not errors and not model_error:
            errors.append("Pi RPC ended before the agent settled")
        terminal_errors = [*errors, *([model_error] if model_error else [])]
        if terminal_errors or not events:
            message = " ".join(terminal_errors) + " " + stderr
            lowered = message.lower()
            if job.macos_sandbox != "off" and "sandbox_apply:" in lowered:
                code = "sandbox_unavailable"
            elif any(token in lowered for token in ("model not found", "unknown model", "model unavailable")):
                code = "model_unavailable"
            elif any(token in lowered for token in ("connection refused", "econnrefused", "fetch failed")):
                code = "model_endpoint_unavailable"
            else:
                code = "pi_rpc_failure"
            raise CodingExecutionError(
                code,
                "agent",
                message.strip()[:1000] or "Pi RPC ended",
                log=log,
                metrics=metrics,
            )
        return AgentOutcome(exit_code=0, metrics=metrics, log=log)


async def _run_argv(
    command: list[str],
    cwd: Path,
    timeout: float,
    cancel_event: asyncio.Event,
    network_policy: str,
    job: CodingJob,
) -> ValidationOutcome:
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["EPHY_NETWORK_POLICY"] = network_policy
    kwargs = (
        {"start_new_session": True} if os.name == "posix"
        else {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    )
    try:
        process = await asyncio.create_subprocess_exec(
            *_macos_write_guard(command, job, cwd),
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=environment,
            **kwargs,
        )
    except OSError as exc:
        if job.macos_sandbox != "off":
            raise CodingExecutionError(
                "sandbox_unavailable", "validation", f"validation sandbox could not start: {type(exc).__name__}"
            ) from None
        return ValidationOutcome(passed=False, exit_code=127, log=f"validation start failed: {type(exc).__name__}\n")
    stdout_task = asyncio.create_task(_read_capped(process.stdout), name="validation-stdout")
    stderr_task = asyncio.create_task(_read_capped(process.stderr), name="validation-stderr")
    wait_task = asyncio.create_task(process.wait(), name="validation-process")
    cancel_task = asyncio.create_task(cancel_event.wait(), name="validation-cancel")
    try:
        done, _ = await asyncio.wait(
            (wait_task, cancel_task), timeout=timeout, return_when=asyncio.FIRST_COMPLETED
        )
        if cancel_task in done and cancel_event.is_set():
            await _stop_process(process)
            raise CodingExecutionError("cancelled", "validation", "cancel requested")
        if wait_task not in done:
            await _stop_process(process)
            raise CodingExecutionError("execution_timeout", "validation", "validation timed out")
        exit_code = await wait_task
    finally:
        cancel_task.cancel()
        with suppress(asyncio.CancelledError):
            await cancel_task
        if process.returncode is None:
            await _stop_process(process)
        if not wait_task.done():
            wait_task.cancel()
            with suppress(asyncio.CancelledError):
                await wait_task
        stdout, stderr = await asyncio.gather(stdout_task, stderr_task)
    log = stdout
    if stderr:
        log += ("\n" if log and not log.endswith("\n") else "") + "[stderr]\n" + stderr
    if job.macos_sandbox != "off" and "sandbox_apply:" in stderr.lower():
        raise CodingExecutionError("sandbox_unavailable", "validation", stderr.strip()[:1000], log=log)
    return ValidationOutcome(passed=exit_code == 0, exit_code=exit_code, log=log)


async def _run_agent_with_limits(
    runner: PiRunner,
    job: CodingJob,
    profile: CodingModelProfile,
    worktree: Path,
    cancel_event: asyncio.Event,
    timeout: float,
) -> AgentOutcome:
    task = asyncio.create_task(runner.run(job, profile, worktree, cancel_event), name="coding-agent")
    cancel_task = asyncio.create_task(cancel_event.wait(), name="coding-agent-cancel")
    try:
        done, _ = await asyncio.wait(
            (task, cancel_task), timeout=timeout, return_when=asyncio.FIRST_COMPLETED
        )
        if cancel_task in done and cancel_event.is_set():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            raise CodingExecutionError("cancelled", "agent", "cancel requested")
        if task not in done:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            raise CodingExecutionError("execution_timeout", "agent", "coding agent timed out")
        return await task
    finally:
        cancel_task.cancel()
        with suppress(asyncio.CancelledError):
            await cancel_task
        if not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task


class CodingArtifactStore:
    def __init__(self, root: Path, run_id: str):
        resolved = output_root(root)
        if not run_id.replace("-", "").replace("_", "").isalnum():
            raise ValueError("invalid coding run ID")
        self.directory = resolved / run_id
        self.directory.mkdir(parents=True, exist_ok=False, mode=0o700)

    def save(
        self,
        result: CodingResult,
        patch: str,
        agent_log: str,
        validation_log: str,
        source_manifest: dict | None = None,
    ) -> None:
        atomic_write(self.directory / "patch.diff", patch)
        atomic_write(self.directory / "agent.log", agent_log)
        atomic_write(self.directory / "validation.log", validation_log)
        if source_manifest is not None:
            atomic_write(
                self.directory / "source-manifest.json",
                json.dumps(source_manifest, ensure_ascii=False, indent=2) + "\n",
            )
        atomic_write(self.directory / "result.json", result.model_dump_json(indent=2) + "\n")


class CodingExecutor:
    def __init__(self, *, runner: PiRunner | None = None):
        self.runner = runner

    @staticmethod
    def _validate_profile(job: CodingJob, profile: CodingModelProfile) -> None:
        if job.model_profile != profile.profile_id:
            raise CodingExecutionError(
                "invalid_model_profile", "profile", "job and selected profile do not match"
            )

    @staticmethod
    def _source(job: CodingJob) -> tuple[Path, str]:
        repository, base = resolve_repository(job.repository_path, job.base_revision)
        if job.source_state == "working-tree":
            head = _git(repository, "rev-parse", "HEAD").stdout.strip()
            if base != head:
                raise CodingExecutionError(
                    "invalid_revision", "repository", "working-tree snapshot requires the current HEAD"
                )
        return repository, base

    def plan(self, job: CodingJob, profile: CodingModelProfile) -> CodingPlan:
        self._validate_profile(job, profile)
        repository, base = self._source(job)
        snapshot_files = None
        if job.source_state == "working-tree":
            snapshot_files = len(WorkingTreeSnapshot(repository, base, job.snapshot_untracked_paths).files())
        runner: PiRunner = self.runner or (
            FakePiRunner() if profile.server_type == "mock" else PiRpcRunner()
        )
        return CodingPlan(
            job_id=job.job_id,
            task_id=job.task_id,
            model=ModelResult(
                profile=profile.profile_id,
                provider=profile.provider,
                model_id=profile.model_id,
                quantization=profile.quantization,
            ),
            repository_path=str(repository),
            base_revision=base,
            worktree=(
                "<worker-owned-temporary-source-snapshot-and-git-worktree>"
                if job.source_state == "working-tree"
                else "<worker-owned-temporary-git-worktree>"
            ),
            pi_invocation=(
                _macos_write_guard(runner.invocation(profile), job)
                if isinstance(runner, PiRpcRunner) else runner.invocation(profile)
            ),
            validation_command=_macos_write_guard(job.validation_command, job),
            timeout_seconds=job.timeout_seconds,
            network_policy=job.network_policy,
            macos_sandbox=job.macos_sandbox,
            macos_protected_roots=sorted(
                str(path) for path in {repository, *(root.expanduser().resolve() for root in job.macos_protected_roots)}
            ) if job.macos_sandbox != "off" else [],
            source_state=job.source_state,
            snapshot_files=snapshot_files,
        )

    async def execute(
        self,
        job: CodingJob,
        profile: CodingModelProfile,
        artifact_root: Path,
        *,
        run_id: str | None = None,
        cancel_event: asyncio.Event | None = None,
    ) -> tuple[CodingResult, Path]:
        run_id = run_id or f"coding-{uuid4().hex}"
        store = CodingArtifactStore(artifact_root, run_id)
        cancel_event = cancel_event or asyncio.Event()
        started = time.monotonic()
        agent = AgentOutcome()
        validation: ValidationOutcome | None = None
        snapshot = PatchSnapshot()
        failure: CodingFailure | None = None
        repository_result = RepositoryResult()
        worktree: GitWorktree | None = None
        source_snapshot: WorkingTreeSnapshot | None = None
        source_manifest: dict | None = None
        cleaned = False
        try:
            self._validate_profile(job, profile)
            _macos_write_guard(job.validation_command, job)
            repository, base = self._source(job)
            repository_result.source_state = job.source_state
            repository_result.source_revision = base
            if job.source_state == "working-tree":
                source_snapshot = WorkingTreeSnapshot(repository, base, job.snapshot_untracked_paths)
                repository, base, source_manifest = source_snapshot.create()
            repository_result.base_revision = base
            worktree = GitWorktree(repository, base)
            path = worktree.create()
            repository_result.worktree_path = str(path)
            runner: PiRunner = self.runner or (
                FakePiRunner() if profile.server_type == "mock" else PiRpcRunner()
            )
            remaining = max(0.001, job.timeout_seconds - (time.monotonic() - started))
            agent = await _run_agent_with_limits(
                runner, job, profile, path, cancel_event, remaining
            )
            snapshot = worktree.snapshot()
            repository_result.resulting_revision = snapshot.resulting_revision
            remaining = job.timeout_seconds - (time.monotonic() - started)
            if remaining <= 0:
                raise CodingExecutionError("execution_timeout", "validation", "job deadline reached")
            validation = await _run_argv(
                job.validation_command, path, remaining, cancel_event, job.network_policy, job
            )
            if not validation.passed:
                failure = CodingFailure(
                    code="validation_failure", stage="validation", detail="validation command failed"
                )
        except CodingExecutionError as exc:
            if exc.log and not agent.log:
                agent.log = exc.log
            if exc.exit_code is not None:
                agent.exit_code = exc.exit_code
            if exc.metrics is not None:
                agent.metrics = exc.metrics
            failure = CodingFailure(code=exc.code, stage=exc.stage, detail=exc.detail)
        except asyncio.CancelledError:
            failure = CodingFailure(code="cancelled", stage="worker", detail="worker task cancelled")
            cancel_event.set()
        except Exception as exc:  # noqa: BLE001 — result must retain a typed internal failure．
            failure = CodingFailure(
                code="internal_worker_failure", stage="worker", detail=type(exc).__name__
            )
        finally:
            if worktree is not None:
                if not snapshot.resulting_revision:
                    with suppress(CodingExecutionError):
                        snapshot = worktree.snapshot()
                        repository_result.resulting_revision = snapshot.resulting_revision
                try:
                    cleaned = worktree.cleanup()
                except CodingExecutionError:
                    cleaned = False
                if cleaned:
                    repository_result.worktree_path = None
                elif failure is None:
                    failure = CodingFailure(
                        code="internal_worker_failure",
                        stage="worktree",
                        detail="temporary worktree cleanup failed",
                    )
            if source_snapshot is not None and cleaned:
                try:
                    source_snapshot.cleanup()
                except OSError:
                    if failure is None:
                        failure = CodingFailure(
                            code="internal_worker_failure",
                            stage="worktree",
                            detail="temporary source snapshot cleanup failed",
                        )

        cancelled = failure is not None and failure.code == "cancelled"
        succeeded = failure is None and validation is not None and validation.passed
        exit_code = (
            validation.exit_code
            if validation is not None
            else agent.exit_code
        )
        result = CodingResult(
            run_id=run_id,
            task_id=job.task_id,
            job_id=job.job_id,
            model=ModelResult(
                profile=profile.profile_id,
                provider=profile.provider,
                model_id=profile.model_id,
                quantization=profile.quantization,
            ),
            repository=repository_result,
            result=ResultStatus(
                status="succeeded" if succeeded else ("cancelled" if cancelled else "failed"),
                validation_passed=validation.passed if validation is not None else None,
                exit_code=exit_code,
                failure=failure,
            ),
            metrics=CodingMetrics(
                wall_time=round(time.monotonic() - started, 6),
                turns=agent.metrics.turns,
                tool_calls=agent.metrics.tool_calls,
                retries=agent.metrics.retries,
                input_tokens=agent.metrics.input_tokens,
                output_tokens=agent.metrics.output_tokens,
                files_changed=len(snapshot.changed_files or []),
                diff_additions=snapshot.additions,
                diff_deletions=snapshot.deletions,
            ),
            changed_files=snapshot.changed_files or [],
            worktree_cleaned=cleaned,
        )
        if source_manifest is not None:
            result.artifacts.source_manifest = "source-manifest.json"
        store.save(
            result,
            snapshot.patch,
            agent.log,
            validation.log if validation is not None else "",
            source_manifest,
        )
        return result, store.directory
