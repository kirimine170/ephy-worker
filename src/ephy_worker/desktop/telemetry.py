"""Read-only host/device and one Pi process measurements.

Process CPU follows psutil's single-core basis: 100% is one logical CPU, so a
multithreaded process may exceed 100%. GPU measurements describe the entire
device, never the worker's allocation. Missing measurements remain ``None``.
"""

from __future__ import annotations

import csv
import io
import math
import shutil
import subprocess
from datetime import UTC, datetime
from typing import Any

import psutil


class MetricsSampler:
    def __init__(self, *, runner=None, psutil_module=None, gpu_binary: str | None = None):
        self._psutil = psutil_module or psutil
        self._runner = runner or subprocess.run
        self._gpu_binary = gpu_binary or shutil.which("nvidia-smi")
        self._host_primed = False
        self._process = None
        self._identity: tuple[int, float] | None = None

    def sample(self, pid: int | None = None) -> dict[str, Any]:
        reasons: dict[str, str] = {}
        result: dict[str, Any] = {
            "sampled_at": datetime.now(UTC).isoformat(),
            "host_cpu_percent": None,
            "host_ram_used_bytes": None,
            "host_ram_total_bytes": None,
            "host_ram_percent": None,
            "worker_process_cpu_percent": None,
            "worker_process_cpu_basis": "single_logical_cpu_100_percent",
            "worker_process_rss_bytes": None,
            "worker_process_pid": None,
            "worker_process_create_time": None,
            "gpu": None,
            "unavailable_reasons": reasons,
        }
        try:
            cpu = self._psutil.cpu_percent(interval=None)
            if self._host_primed:
                result["host_cpu_percent"] = cpu
            else:
                reasons["host_cpu_percent"] = "first_sample"
            self._host_primed = True
        except (OSError, self._psutil.Error):
            self._host_primed = False
            reasons["host_cpu_percent"] = "measurement_failed"
        try:
            memory = self._psutil.virtual_memory()
            result.update(
                host_ram_used_bytes=memory.used,
                host_ram_total_bytes=memory.total,
                host_ram_percent=memory.percent,
            )
        except (OSError, self._psutil.Error):
            reasons["host_ram"] = "measurement_failed"
        self._sample_process(pid, result, reasons)
        result["gpu"] = self._sample_gpu(reasons)
        return result

    def _sample_process(self, pid, result, reasons):
        if pid is None:
            self._identity = None
            self._process = None
            reasons["worker_process"] = "not_running"
            return
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
            reasons["worker_process"] = "invalid_pid"
            return
        try:
            fresh = self._psutil.Process(pid)
            identity = (pid, fresh.create_time())
            if self._identity is not None and self._identity[0] == pid and self._identity != identity:
                reasons["worker_process"] = "process_identity_changed"
                return
            first = self._identity != identity
            if first:
                self._identity, self._process = identity, fresh
            process = self._process
            if not process.is_running():
                reasons["worker_process"] = "process_exited"
                return
            cpu, rss = process.cpu_percent(interval=None), process.memory_info().rss
            if not process.is_running():
                reasons["worker_process"] = "process_exited"
                return
            result.update(
                worker_process_pid=pid,
                worker_process_create_time=identity[1],
                worker_process_cpu_percent=None if first else cpu,
                worker_process_rss_bytes=rss,
            )
            if first:
                reasons["worker_process_cpu_percent"] = "first_sample"
        except self._psutil.AccessDenied:
            reasons["worker_process"] = "access_denied"
        except self._psutil.NoSuchProcess:
            reasons["worker_process"] = "process_exited"
        except (OSError, self._psutil.Error):
            reasons["worker_process"] = "measurement_failed"

    def _sample_gpu(self, reasons):
        if not self._gpu_binary:
            reasons["gpu"] = "nvidia_smi_unavailable"
            return None
        try:
            response = self._runner(
                [
                    self._gpu_binary,
                    "--query-gpu=index,name,utilization.gpu,memory.used,memory.total",
                    "--format=csv,noheader,nounits",
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                shell=False,
                timeout=1.0,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if response.returncode != 0:
                reasons["gpu"] = "nvidia_smi_failed"
                return None
            devices = []
            for row in csv.reader(io.StringIO(response.stdout)):
                if not row:
                    continue
                if len(row) != 5:
                    raise ValueError("invalid_gpu_row")
                index, name, utilization, used, total = (cell.strip() for cell in row)
                device = {
                    "index": int(index),
                    "name": name,
                    "scope": "host_device",
                    "utilization_percent": self._gpu_number(utilization),
                    "vram_used_bytes": self._gpu_number(used, mib=True),
                    "vram_total_bytes": self._gpu_number(total, mib=True),
                }
                for key in ("utilization_percent", "vram_used_bytes", "vram_total_bytes"):
                    if device[key] is None:
                        reasons[f"gpu.{index}.{key}"] = "driver_value_unavailable"
                devices.append(device)
            if not devices:
                reasons["gpu"] = "no_devices_reported"
                return None
            return devices
        except subprocess.TimeoutExpired:
            reasons["gpu"] = "nvidia_smi_timeout"
        except (OSError, ValueError, csv.Error):
            reasons["gpu"] = "nvidia_smi_unavailable_or_invalid"
        return None

    @staticmethod
    def _gpu_number(value: str, *, mib: bool = False) -> float | int | None:
        try:
            number = float(value)
        except ValueError:
            return None
        if not math.isfinite(number) or number < 0 or (not mib and number > 100):
            return None
        return int(number * 1024 * 1024) if mib else number
