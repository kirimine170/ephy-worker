#!/usr/bin/env python3
"""Read-only, offline host preflight; never a worker registration or job result."""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import importlib.metadata
import json
import os
import platform
import re
import shutil
import stat
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

VERSION = "0.1.0"
SCHEMA_VERSION = "ephy.node-preflight.v1"
SAMPLE_VERSION = "ephy.node-preflight.sample.v1"
REFERENCE_COMMIT = "0f6c3cb7a272bc67e2a0a0dc828e579df6823d98"
REFERENCE_URL = f"https://github.com/kirimine170/ephy-worker/tree/{REFERENCE_COMMIT}"
GIB = 1024 ** 3
MAX_INPUT_BYTES = 65536
MAX_SOURCE_BYTES = 131072
MAX_INTEGER = 2 ** 63 - 1
STATES = {"observed", "unavailable", "unknown", "not_checked"}
REASONS = {
    "system_api", "path_lookup_only", "distribution_metadata_only", "explicit_workspace",
    "fixed_source_hashes", "not_requested", "unsupported_platform", "not_found",
    "permission_denied", "probe_error", "invalid_value", "field_not_reported",
    "unsafe_source_path", "source_too_large", "sample", "non_release_version",
}
TOOLS = ("git", "uv", "node", "npm", "pdftoppm", "pdftotext")
# Direct dependency bounds from the pinned pyproject.toml. Extras and transitive
# dependency resolution are deliberately NOT certified by metadata presence.
RUNTIME_REQUIREMENTS = {
    "pydantic-ai-slim": ((1, 0), (2,)),
    "pydantic": ((2, 11), (3,)),
    "httpx": ((0, 28), (0, 29)),
    "aiohttp": ((3, 12), (4,)),
    "PyYAML": ((6, 0), (7,)),
    "trafilatura": ((2, 0), (3,)),
    "pypdf": ((6, 0), (7,)),
    "psutil": ((7, 0), (8,)),
}
DEV_REQUIREMENTS = {
    "pytest": ((8, 4), (10,)),
    "pytest-asyncio": ((1, 0), (2,)),
    "reportlab": ((4, 4), (5,)),
    "ruff": ((0, 12), (1,)),
}
PACKAGES = {**RUNTIME_REQUIREMENTS, **DEV_REQUIREMENTS}
REFERENCE_HASHES = {
    "src/ephy_worker/contracts.py": "b65e8b092df304297837161a483e0a4777be0f677de1a1cb537b757007062b13",
    "src/ephy_worker/worker_service.py": "aa675757ce20c5e82e22fa49c47680cd6bd759a71a7af25143bd3609f2a5feba",
    "pyproject.toml": "e071db180cf70c9fddbbc3b93151fd9fab65bf5c23903a63672d9ed7ce9a30ae",
}
BASE_TYPES = {
    "os": "os", "architecture": "arch", "python_version": "python",
    "cpu_logical": "positive_int", "cpu_affinity": "positive_int",
    "memory_total_bytes": "positive_int", "memory_available_bytes": "nonnegative_int",
    "workspace_directory": "bool", "workspace_access_hint": "bool",
    "disk_total_bytes": "positive_int", "disk_free_bytes": "nonnegative_int",
    "source_reference_match": "bool",
}
OBSERVATION_TYPES = {
    **BASE_TYPES,
    **{f"tool.{name}": "bool" for name in TOOLS},
    **{f"package.{name}": "version" for name in PACKAGES},
}
VERSION_RE = re.compile(r"[0-9]{1,6}(?:\.[0-9]{1,6}){0,3}\Z")


class InputError(ValueError):
    """Fixed error codes only: do not echo paths, source or external error text."""


def observation(status: str, value: Any = None, reason: str = "system_api") -> dict:
    return {"status": status, "value": value, "reason": reason}


def observed(value: Any, reason: str = "system_api") -> dict:
    return observation("observed", value, reason)


def unknown(reason: str = "probe_error") -> dict:
    return observation("unknown", reason=reason)


def positive_measurement(value: Any) -> dict:
    if type(value) is int and 0 < value <= MAX_INTEGER:
        return observed(value)
    return unknown("invalid_value")


def normalize_architecture(value: str) -> str:
    value = value.lower()
    if value in {"amd64", "x86_64"}:
        return "x86_64"
    if value in {"aarch64", "arm64"}:
        return "arm64"
    if value in {"i386", "i486", "i586", "i686", "x86"}:
        return "x86"
    if value.startswith("arm"):
        return "arm"
    return "other" if value else "unknown"


def memory_linux() -> tuple[dict, dict]:
    """Read exactly one bounded system file; never infer availability from free."""
    try:
        with open("/proc/meminfo", "rb") as handle:
            data = handle.read(MAX_INPUT_BYTES + 1)
        if len(data) > MAX_INPUT_BYTES:
            return unknown("invalid_value"), unknown("invalid_value")
        values = {}
        for line in data.decode("ascii").splitlines():
            match = re.fullmatch(r"(MemTotal|MemAvailable):\s+([0-9]+) kB", line)
            if match:
                values[match[1]] = int(match[2]) * 1024
        total = positive_measurement(values.get("MemTotal"))
        available = values.get("MemAvailable")
        avail = observed(available) if type(available) is int and 0 <= available <= MAX_INTEGER else unknown("field_not_reported")
        if total["status"] == "observed" and avail["status"] == "observed" and available > total["value"]:
            avail = unknown("invalid_value")
        return total, avail
    except (OSError, UnicodeError, ValueError):
        return unknown(), unknown()


def memory_windows() -> tuple[dict, dict]:
    class MemoryStatus(ctypes.Structure):
        _fields_ = [
            ("length", ctypes.c_uint32), ("load", ctypes.c_uint32),
            ("total_physical", ctypes.c_uint64), ("available_physical", ctypes.c_uint64),
            ("total_page", ctypes.c_uint64), ("available_page", ctypes.c_uint64),
            ("total_virtual", ctypes.c_uint64), ("available_virtual", ctypes.c_uint64),
            ("available_extended", ctypes.c_uint64),
        ]
    try:
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        call = kernel.GlobalMemoryStatusEx
        call.argtypes = [ctypes.POINTER(MemoryStatus)]
        call.restype = ctypes.c_int
        status = MemoryStatus()
        status.length = ctypes.sizeof(status)
        if not call(ctypes.byref(status)):
            return unknown(), unknown()
        total = positive_measurement(status.total_physical)
        available = int(status.available_physical)
        avail = observed(available) if total["status"] == "observed" and 0 <= available <= total["value"] else unknown("invalid_value")
        return total, avail
    except (OSError, AttributeError):
        return unknown(), unknown()


def memory_macos() -> tuple[dict, dict]:
    # Native fixed libSystem API; no shell, sysctl executable, vm_stat or guessed
    # "available" calculation. Reclaimable/compressed pages need a separate policy.
    try:
        library = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
        call = library.sysctlbyname
        call.argtypes = [ctypes.c_char_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_size_t), ctypes.c_void_p, ctypes.c_size_t]
        call.restype = ctypes.c_int
        value = ctypes.c_uint64()
        size = ctypes.c_size_t(ctypes.sizeof(value))
        ok = call(b"hw.memsize", ctypes.byref(value), ctypes.byref(size), None, 0)
        if ok != 0 or size.value != ctypes.sizeof(value):
            return unknown(), unknown("field_not_reported")
        return positive_measurement(value.value), unknown("field_not_reported")
    except (OSError, AttributeError):
        return unknown(), unknown("field_not_reported")


def memory_for_os(system: str) -> tuple[dict, dict]:
    probe = {"Linux": memory_linux, "Windows": memory_windows, "Darwin": memory_macos}.get(system)
    return probe() if probe else (unknown("unsupported_platform"), unknown("unsupported_platform"))


def _read_fd(fd: int, limit: int) -> bytes:
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        raise InputError("input_not_regular_file")
    chunks = []
    count = 0
    while count <= limit:
        data = os.read(fd, min(65536, limit + 1 - count))
        if not data:
            break
        chunks.append(data)
        count += len(data)
    if count > limit:
        raise InputError("input_too_large")
    return b"".join(chunks)


def _read_windows_regular(path: Path, limit: int) -> bytes:
    """Open the explicitly named sample file without following final reparse points."""
    library = ctypes.WinDLL("kernel32", use_last_error=True)
    create = library.CreateFileW
    create.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]
    create.restype = ctypes.c_void_p
    close = library.CloseHandle
    close.argtypes = [ctypes.c_void_p]
    close.restype = ctypes.c_int
    get_type = library.GetFileType
    get_type.argtypes = [ctypes.c_void_p]
    get_type.restype = ctypes.c_uint32
    info_call = library.GetFileInformationByHandleEx
    info_call.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
    info_call.restype = ctypes.c_int
    read = library.ReadFile
    read.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p]
    read.restype = ctypes.c_int
    # FILE_SHARE_READ excludes concurrent writes/deletes while the handle is held.
    handle = create(str(path), 0x80000000, 1, None, 3, 0x00200000, None)
    if handle == ctypes.c_void_p(-1).value or handle is None:
        error = ctypes.get_last_error()
        raise InputError({2: "input_not_found", 3: "input_not_found", 5: "input_permission_denied"}.get(error, "input_unreadable"))
    try:
        attributes = (ctypes.c_uint32 * 2)()
        if get_type(handle) != 1 or not info_call(handle, 9, ctypes.byref(attributes), ctypes.sizeof(attributes)):
            raise InputError("input_not_regular_file")
        if attributes[0] & (0x10 | 0x400):  # DIRECTORY or REPARSE_POINT
            raise InputError("input_not_regular_file")
        buffer = ctypes.create_string_buffer(limit + 1)
        count = ctypes.c_uint32()
        if not read(handle, buffer, limit + 1, ctypes.byref(count), None):
            raise InputError("input_unreadable")
        if count.value > limit:
            raise InputError("input_too_large")
        return buffer.raw[:count.value]
    finally:
        close(handle)


def read_bounded_regular(path: Path, limit: int) -> bytes:
    """Bounded explicit file read; refuse special files and final symlinks."""
    try:
        if os.name == "nt":
            return _read_windows_regular(path, limit)
        if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_NONBLOCK"):
            raise InputError("safe_file_read_unsupported")
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
        fd = os.open(path, flags)
        try:
            return _read_fd(fd, limit)
        finally:
            os.close(fd)
    except FileNotFoundError:
        raise InputError("input_not_found") from None
    except PermissionError:
        raise InputError("input_permission_denied") from None
    except (OSError, AttributeError):
        raise InputError("input_unreadable") from None


def _read_source_at(root_fd: int, relative: str) -> bytes:
    """Each component is opened relative to a held directory fd, never followed."""
    current = os.dup(root_fd)
    try:
        parts = Path(relative).parts
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=current)
            os.close(current)
            current = child
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=current)
        try:
            return _read_fd(fd, MAX_SOURCE_BYTES)
        finally:
            os.close(fd)
    finally:
        os.close(current)


def source_reference_probe(root: Path | None) -> dict:
    if root is None:
        return observation("not_checked", reason="not_requested")
    # Python's standard Windows file API has no descriptor-relative traversal.
    # Do not risk following an ancestor junction/reparse point or a path swap.
    if os.name != "posix" or os.open not in os.supports_dir_fd or not all(hasattr(os, key) for key in ("O_DIRECTORY", "O_NOFOLLOW", "O_NONBLOCK")):
        return unknown("unsupported_platform")
    try:
        root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            matches = True
            for relative, expected in REFERENCE_HASHES.items():
                data = _read_source_at(root_fd, relative)
                normalized = data.replace(b"\r\n", b"\n")
                matches = matches and hashlib.sha256(normalized).hexdigest() == expected
            return observed(matches, "fixed_source_hashes")
        finally:
            os.close(root_fd)
    except InputError as exc:
        reason = "source_too_large" if str(exc) == "input_too_large" else "unsafe_source_path"
        return unknown(reason)
    except FileNotFoundError:
        return observation("unavailable", reason="not_found")
    except PermissionError:
        return unknown("permission_denied")
    except (OSError, RuntimeError):
        return unknown("unsafe_source_path")


def workspace_probe(workspace: Path) -> dict:
    result = {key: unknown() for key in ("workspace_directory", "workspace_access_hint", "disk_total_bytes", "disk_free_bytes")}
    try:
        info = workspace.stat()
        result["workspace_directory"] = observed(stat.S_ISDIR(info.st_mode), "explicit_workspace")
        if not stat.S_ISDIR(info.st_mode):
            result["workspace_access_hint"] = observation("unavailable", reason="not_found")
            return result
        result["workspace_access_hint"] = observed(os.access(workspace, os.W_OK | os.X_OK), "explicit_workspace")
        try:
            usage = shutil.disk_usage(workspace)
            result["disk_total_bytes"] = positive_measurement(usage.total)
            if 0 <= usage.free <= usage.total:
                result["disk_free_bytes"] = observed(usage.free, "explicit_workspace")
        except OSError:
            pass  # Explicit unknown values above retain the failed measurement.
    except FileNotFoundError:
        result["workspace_directory"] = observation("unavailable", reason="not_found")
    except PermissionError:
        result["workspace_directory"] = unknown("permission_denied")
    except (OSError, ValueError):
        pass  # No raw exception text/path is exposed.
    return result


def package_probe(name: str) -> dict:
    try:
        version = importlib.metadata.version(name)
        if type(version) is not str:
            return unknown("invalid_value")
        if not VERSION_RE.fullmatch(version):
            return unknown("non_release_version")
        return observed(version, "distribution_metadata_only")
    except importlib.metadata.PackageNotFoundError:
        return observation("unavailable", reason="not_found")
    except (OSError, ValueError, UnicodeError, TypeError, KeyError):
        return unknown()


def collect_observations(workspace: Path, worker_source: Path | None = None) -> dict:
    system = platform.system()
    obs = {
        "os": observed(system if system in {"Linux", "Windows", "Darwin"} else "Other"),
        "architecture": observed(normalize_architecture(platform.machine())),
        "python_version": observed(list(sys.version_info[:3])),
        "cpu_logical": positive_measurement(os.cpu_count()),
        "cpu_affinity": unknown("unsupported_platform"),
    }
    try:
        if hasattr(os, "sched_getaffinity"):
            obs["cpu_affinity"] = positive_measurement(len(os.sched_getaffinity(0)))
    except OSError:
        obs["cpu_affinity"] = unknown()
    obs["memory_total_bytes"], obs["memory_available_bytes"] = memory_for_os(system)
    obs.update(workspace_probe(workspace))
    obs["source_reference_match"] = source_reference_probe(worker_source)
    for name in TOOLS:
        try:
            obs[f"tool.{name}"] = observed(shutil.which(name) is not None, "path_lookup_only")
        except OSError:
            obs[f"tool.{name}"] = unknown()
    for name in PACKAGES:
        obs[f"package.{name}"] = package_probe(name)
    validate_observations(obs)
    return obs


def valid_value(kind: str, value: Any) -> bool:
    if kind == "bool":
        return type(value) is bool
    if kind.endswith("int"):
        return type(value) is int and (1 if kind == "positive_int" else 0) <= value <= MAX_INTEGER
    if kind == "os":
        return value in ("Linux", "Windows", "Darwin", "Other") if type(value) is str else False
    if kind == "arch":
        return value in ("x86_64", "arm64", "x86", "arm", "other", "unknown") if type(value) is str else False
    if kind == "python":
        return type(value) is list and len(value) == 3 and all(type(n) is int and 0 <= n <= 99999 for n in value)
    if kind == "version":
        return type(value) is str and VERSION_RE.fullmatch(value) is not None
    return False


def validate_observations(obs: Any) -> None:
    if type(obs) is not dict or set(obs) != set(OBSERVATION_TYPES):
        raise InputError("invalid_observation_keys")
    for key, kind in OBSERVATION_TYPES.items():
        item = obs[key]
        if type(item) is not dict or set(item) != {"status", "value", "reason"}:
            raise InputError("invalid_observation_shape")
        if type(item["status"]) is not str or item["status"] not in STATES:
            raise InputError("invalid_observation_status")
        if type(item["reason"]) is not str or item["reason"] not in REASONS:
            raise InputError("invalid_observation_reason")
        if item["status"] == "observed":
            if not valid_value(kind, item["value"]):
                raise InputError("invalid_observation_value")
        elif item["value"] is not None:
            raise InputError("nonobserved_value_must_be_null")
    for lower, upper in (("memory_available_bytes", "memory_total_bytes"), ("disk_free_bytes", "disk_total_bytes")):
        if obs[lower]["status"] == obs[upper]["status"] == "observed" and obs[lower]["value"] > obs[upper]["value"]:
            raise InputError("inconsistent_measurements")


def unique_object(pairs: list) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise InputError("duplicate_json_key")
        result[key] = value
    return result


def reject_constant(value: str) -> None:
    raise InputError("nonfinite_json_number")


def load_sample(path: Path) -> dict:
    try:
        data = json.loads(read_bounded_regular(path, MAX_INPUT_BYTES).decode("utf-8"), object_pairs_hook=unique_object, parse_constant=reject_constant)
    except (UnicodeError, json.JSONDecodeError, RecursionError, ValueError) as exc:
        if isinstance(exc, InputError):
            raise
        raise InputError("invalid_sample_json") from None
    if type(data) is not dict or set(data) != {"schema_version", "observations"} or data["schema_version"] != SAMPLE_VERSION:
        raise InputError("invalid_sample_schema")
    validate_observations(data["observations"])
    return data["observations"]


def padded_version(version: str | tuple) -> tuple:
    values = tuple(int(v) for v in version.split(".")) if isinstance(version, str) else version
    return values + (0,) * (4 - len(values))


def check(obs: dict, key: str, predicate, failure: str, requirement: str) -> dict:
    item = obs[key]
    state = item["status"]
    if state == "observed":
        status, reason = ("pass", "observed_prerequisite") if predicate(item["value"]) else ("fail", failure)
    elif state == "unavailable":
        status, reason = "fail", "required_item_unavailable"
    else:
        status, reason = state, item["reason"]
    return {"id": key, "status": status, "reason": reason, "requirement": requirement}


def profile_result(name: str, obs: dict, min_free_bytes: int, min_available_bytes: int | None) -> dict:
    checks = [
        check(obs, "os", lambda v: v in {"Linux", "Windows", "Darwin"}, "unsupported_os", "Windows/Linux/macOS"),
        check(obs, "python_version", lambda v: (3, 12) <= tuple(v[:2]) < (3, 15), "python_outside_reference_range", ">=3.12,<3.15"),
        check(obs, "workspace_directory", bool, "not_a_directory", "explicit existing directory"),
        check(obs, "workspace_access_hint", bool, "access_hint_denied", "os.access hint only; no write test"),
        check(obs, "disk_free_bytes", lambda v: v >= min_free_bytes, "below_requested_disk_threshold", f">={min_free_bytes} bytes; planning threshold"),
    ]
    if min_available_bytes is not None:
        checks.append(check(obs, "memory_available_bytes", lambda v: v >= min_available_bytes, "below_requested_memory_threshold", f">={min_available_bytes} bytes; planning threshold"))
    if name in {"collect", "test"}:
        checks.append(check(obs, "source_reference_match", bool, "reference_source_changed", "three fixed source files match reference; not whole checkout attestation"))
        required = dict(RUNTIME_REQUIREMENTS)
        if name == "test":
            required.update(DEV_REQUIREMENTS)
            checks.append(check(obs, "tool.git", bool, "tool_not_on_path", "Git on PATH; not executed"))
    else:
        required = {"reportlab": DEV_REQUIREMENTS["reportlab"]}
        for tool in ("pdftoppm", "pdftotext"):
            checks.append(check(obs, f"tool.{tool}", bool, "tool_not_on_path", "PDF inspection executable on PATH; not executed"))
    for package, (minimum, maximum) in required.items():
        checks.append(check(obs, f"package.{package}", lambda v, low=minimum, high=maximum: padded_version(low) <= padded_version(v) < padded_version(high), "package_outside_reference_range", ">=" + ".".join(map(str, minimum)) + ",<" + ".".join(map(str, maximum)) + "; metadata only"))
    states = {entry["status"] for entry in checks}
    status = "blocked" if "fail" in states else "incomplete" if states & {"unknown", "not_checked"} else "preflight_passed"
    return {
        "profile": name, "status": status, "checks": checks,
        "meaning": {"collect": "local prerequisites for web.collect; connectivity and jobs not checked", "test": "local ephy-worker development prerequisites; tests not executed", "render": "local PDF/report inspection prerequisites; no rendering executed"}[name],
        "dispatch_eligible": False,
    }


def build_report(obs: dict, profiles: list[str], mode: str, min_free_bytes: int = GIB, min_available_bytes: int | None = None) -> dict:
    validate_observations(obs)
    if mode not in {"live", "sample"} or not profiles or any(name not in {"collect", "test", "render"} for name in profiles):
        raise InputError("invalid_report_options")
    if type(min_free_bytes) is not int or not 0 <= min_free_bytes <= MAX_INTEGER or (min_available_bytes is not None and (type(min_available_bytes) is not int or not 0 <= min_available_bytes <= MAX_INTEGER)):
        raise InputError("invalid_threshold")
    results = [profile_result(name, obs, min_free_bytes, min_available_bytes) for name in dict.fromkeys(profiles)]
    statuses = {result["status"] for result in results}
    overall = "blocked" if "blocked" in statuses else "incomplete" if "incomplete" in statuses else "preflight_passed"
    return {
        "schema_version": SCHEMA_VERSION, "tool_version": VERSION, "mode": mode,
        "overall": overall, "dispatch_eligible": False, "advertised_capabilities": [],
        "reference": {"repository": "kirimine170/ephy-worker", "commit": REFERENCE_COMMIT, "url": REFERENCE_URL, "contract_version": "0.4", "distributed_capability": "web.collect", "collect_modes": ["search", "extract"], "worker_concurrency": 1, "source_match_scope": sorted(REFERENCE_HASHES)},
        "thresholds": {"min_free_bytes": min_free_bytes, "min_available_bytes": min_available_bytes, "origin": "caller planning policy; not repository hardware requirements"},
        "observations": obs, "profiles": results,
        "not_checked": ["workspace_write_and_git_boundary", "tool_execution_and_versions", "dependency_imports_extras_transitives_and_lockfile", "worker_config_and_credentials", "manager_search_tls_and_tunnel_connectivity", "worker_registration_and_job_execution", "test_and_render_execution", "gpu_devices_drivers_vram_and_model_capacity", "cpu_memory_and_disk_quotas", "sandbox_and_isolation"],
        "privacy": {"paths_emitted": False, "credentials_read": False, "environment_dumped": False, "network_requests_started": False, "subprocesses_started": False, "files_written": False},
    }


def summary(report: dict) -> str:
    obs = report["observations"]
    def value(key):
        item = obs[key]
        return str(item["value"]) if item["status"] == "observed" else item["status"]
    def gib(key):
        item = obs[key]
        return f"{item['value'] / GIB:.2f} GiB" if item["status"] == "observed" else item["status"]
    lines = [
        f"ephy-node-preflight {VERSION} | {report['mode']} | {report['overall']}",
        f"OS: {value('os')} / {value('architecture')} | Python: {value('python_version')}",
        f"CPU logical: {value('cpu_logical')} | affinity: {value('cpu_affinity')} | quotas: not_checked",
        f"RAM total: {gib('memory_total_bytes')} | available: {gib('memory_available_bytes')}",
        f"Workspace free: {gib('disk_free_bytes')} | access hint: {value('workspace_access_hint')}",
        "PATH presence only: " + ", ".join(f"{name}={value(f'tool.{name}')}" for name in TOOLS),
    ]
    for profile in report["profiles"]:
        lines.append(f"{profile['profile']}: {profile['status']}")
        for entry in profile["checks"]:
            if entry["status"] != "pass":
                lines.append(f"  {entry['status']}: {entry['id']} ({entry['reason']})")
    lines.extend([
        "Reference: web.collect / contract 0.4 / search,extract / concurrency 1",
        "No registration, jobs, tests, renders, connectivity or GPU/model capacity verified.",
        "preflight_passed = observed local prerequisites only; dispatch_eligible = false.",
    ])
    if report["mode"] == "sample":
        lines.append("SAMPLE: synthetic input; no statement about this host.")
    return "\n".join(lines)


def gib_argument(raw: str) -> int:
    try:
        value = Decimal(raw)
        if not value.is_finite() or not 0 <= value <= 1048576:
            raise ValueError
        return int(value * GIB)
    except (InvalidOperation, ValueError, OverflowError):
        raise argparse.ArgumentTypeError("GiBは0〜1048576の有限数で指定してください") from None


class RedactingParser(argparse.ArgumentParser):
    def error(self, message):
        # argparse errors otherwise echo unknown tokens and paths verbatim.
        self.exit(2, '{"error":"invalid_arguments"}\n')


def parser() -> argparse.ArgumentParser:
    root = RedactingParser(description="ephy-worker用の読み取り専用・オフライン準備確認．登録／通信／Job実行は行いません．", epilog="終了値: 0=ローカル前提の確認通過，1=不足あり，2=入力／診断エラー，3=未確認あり，130=取消．0でも運用可能性は未検証です．")
    inputs = root.add_mutually_exclusive_group(required=True)
    inputs.add_argument("--workspace", type=Path, help="容量を調べる既存directory．作成・書込・再帰走査はしません")
    inputs.add_argument("--sample", type=Path, help="固定形式JSONを使用．hostのprobeを一切行いません")
    root.add_argument("--worker-source", type=Path, help="ephy-worker root．固定3ファイルのhashだけ確認（collect/test向け）")
    root.add_argument("--profile", action="append", choices=("collect", "test", "render"), help="複数指定可．既定collect．renderはPDF確認用でworker能力ではありません")
    root.add_argument("--min-free-gib", type=gib_argument, default=GIB, help="計画上の空き容量下限．既定1 GiB，repoの必須容量ではありません")
    root.add_argument("--min-available-gib", type=gib_argument, help="任意の利用可能RAM下限．macOS等の不明値はincomplete")
    root.add_argument("--format", choices=("summary", "json", "both"), default="summary", help="bothはJSONをstdout，summaryをstderrへ出力")
    return root


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="strict")
    cli = parser()
    args = cli.parse_args(argv)
    if args.sample is not None and args.worker_source is not None:
        cli.error("--sampleと--worker-sourceは併用できません")
    try:
        mode = "sample" if args.sample is not None else "live"
        obs = load_sample(args.sample) if mode == "sample" else collect_observations(args.workspace, args.worker_source)
        report = build_report(obs, args.profile or ["collect"], mode, args.min_free_gib, args.min_available_gib)
        if args.format in {"json", "both"}:
            print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False))
        if args.format in {"summary", "both"}:
            print(summary(report), file=sys.stderr if args.format == "both" else sys.stdout)
        return {"preflight_passed": 0, "blocked": 1, "incomplete": 3}[report["overall"]]
    except InputError as exc:
        print(json.dumps({"error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print('{"error":"cancelled"}', file=sys.stderr)
        return 130
    except (OSError, RuntimeError, ValueError):
        print('{"error":"probe_failed"}', file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
