import subprocess
from types import SimpleNamespace

import psutil

from ephy_worker.desktop.telemetry import MetricsSampler


class FakeProcess:
    def __init__(self, created=10.0):
        self.created = created
        self.alive = True

    def create_time(self):
        return self.created

    def is_running(self):
        return self.alive

    def cpu_percent(self, interval=None):
        return 150.0

    def memory_info(self):
        return SimpleNamespace(rss=4096)


class FakePsutil:
    Error = psutil.Error
    AccessDenied = psutil.AccessDenied
    NoSuchProcess = psutil.NoSuchProcess

    def __init__(self):
        self.process = FakeProcess()

    def cpu_percent(self, interval=None):
        return 25.0

    def virtual_memory(self):
        return SimpleNamespace(used=1024, total=4096, percent=25.0)

    def Process(self, pid):
        return self.process


def sampler(ps=None, output="0, GPU Example, 42, 512, 2048\n"):
    return MetricsSampler(
        psutil_module=ps or FakePsutil(),
        gpu_binary="nvidia-smi.exe",
        runner=lambda *a, **kw: SimpleNamespace(returncode=0, stdout=output),
    )


def test_first_cpu_samples_are_unavailable_then_measured():
    subject = sampler()
    first = subject.sample(42)
    assert first["host_cpu_percent"] is None
    assert first["worker_process_cpu_percent"] is None
    assert first["worker_process_rss_bytes"] == 4096
    second = subject.sample(42)
    assert second["host_cpu_percent"] == 25.0
    assert second["worker_process_cpu_percent"] == 150.0
    assert second["worker_process_cpu_basis"] == "single_logical_cpu_100_percent"
    assert second["host_ram_total_bytes"] == 4096
    assert second["gpu"][0]["vram_used_bytes"] == 512 * 1024 * 1024
    assert second["gpu"][0]["scope"] == "host_device"
    assert "worker_vram" not in second


def test_process_identity_change_stays_unavailable_until_rebound():
    ps = FakePsutil()
    subject = sampler(ps)
    subject.sample(42)
    ps.process = FakeProcess(created=11.0)
    for _ in range(2):
        result = subject.sample(42)
        assert result["worker_process_rss_bytes"] is None
        assert result["worker_process_cpu_percent"] is None
        assert result["unavailable_reasons"]["worker_process"] == "process_identity_changed"
    subject.sample(None)
    rebound = subject.sample(42)
    assert rebound["worker_process_create_time"] == 11.0
    assert rebound["worker_process_cpu_percent"] is None


def test_process_exit_and_access_denial_do_not_invent_zero():
    ps = FakePsutil()
    subject = sampler(ps)
    subject.sample(42)
    ps.process.alive = False
    result = subject.sample(42)
    assert result["worker_process_rss_bytes"] is None
    assert result["unavailable_reasons"]["worker_process"] == "process_exited"

    def denied(pid):
        raise psutil.AccessDenied(pid)

    ps.Process = denied
    assert subject.sample(43)["unavailable_reasons"]["worker_process"] == "access_denied"


def test_driver_unsupported_fields_and_invalid_rows_are_unavailable():
    result = sampler(output="0, GPU, [N/A], N/A, 2048\n").sample()
    assert result["gpu"][0]["utilization_percent"] is None
    assert result["gpu"][0]["vram_used_bytes"] is None
    assert "gpu.0.utilization_percent" in result["unavailable_reasons"]
    assert sampler(output="unexpected\n").sample()["gpu"] is None
    assert sampler(output="0, GPU, NaN, -1, 2048\n").sample()["gpu"][0]["utilization_percent"] is None


def test_gpu_invocation_is_fixed_hidden_read_only_and_bounded():
    calls = []

    def timeout(argv, **kwargs):
        calls.append((argv, kwargs))
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    result = MetricsSampler(
        runner=timeout, psutil_module=FakePsutil(), gpu_binary="nvidia-smi.exe"
    ).sample()
    argv, options = calls[0]
    assert argv == [
        "nvidia-smi.exe", "--query-gpu=index,name,utilization.gpu,memory.used,memory.total",
        "--format=csv,noheader,nounits",
    ]
    assert options["shell"] is False
    assert options["timeout"] == 1.0
    assert options["creationflags"] == getattr(subprocess, "CREATE_NO_WINDOW", 0)
    assert result["gpu"] is None
    assert result["unavailable_reasons"]["gpu"] == "nvidia_smi_timeout"


def test_missing_gpu_and_process_are_explicit(monkeypatch):
    monkeypatch.setattr("ephy_worker.desktop.telemetry.shutil.which", lambda _: None)
    result = MetricsSampler(psutil_module=FakePsutil()).sample()
    assert result["gpu"] is None
    assert result["worker_process_pid"] is None
    assert result["unavailable_reasons"]["gpu"] == "nvidia_smi_unavailable"
    assert result["unavailable_reasons"]["worker_process"] == "not_running"
