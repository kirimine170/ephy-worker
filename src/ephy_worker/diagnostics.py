"""Read-only connection diagnostics，using the same bounded provider paths．"""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryFile
from typing import Literal

from pydantic import Field

from .budget import Budget, BudgetExceeded
from .config import ConfigurationError, WorkerConfig
from .models import ModelError, ModelRunner
from .schema import Extraction, StrictModel
from .search import SearchError, create_search_provider


class _Probe(StrictModel):
    answer: Literal["ok"]
    value: int = Field(ge=4, le=4)


async def doctor(
    config: WorkerConfig, profile_id: str | None = None, output_dir: str | Path | None = None
) -> dict:
    budget = Budget(config.limits)
    result: dict = {"ok": True, "search": {}, "profiles": {}, "output": None}
    search = None
    try:
        search = create_search_provider(config.search, budget)
        result["search"] = await search.engine_health()
        if not result["search"]["enabled"]:
            raise ValueError("searxng_engine_disabled")
        candidates = await search.search("Python documentation", "doctor-public-probe")
        result["search"].update(
            ok=True, json_api=True, probe_result_count=len(candidates), diagnostic=search.last_diagnostic
        )
    except (SearchError, ConfigurationError, BudgetExceeded, ValueError) as exc:
        result["ok"] = False
        result["search"].update(ok=False, error=getattr(exc, "code", type(exc).__name__))
        if search:
            result["search"]["diagnostic"] = search.last_diagnostic
    finally:
        if search:
            await search.aclose()
    selected = {profile_id: config.model_profiles.get(profile_id)} if profile_id else config.model_profiles
    for name, profile in selected.items():
        runner = None
        try:
            if profile is None:
                raise ValueError("unknown_profile")
            runner = ModelRunner(profile, budget)
            result["profiles"][name] = {
                "metadata": runner.metadata,
                "basic_output_probe": False,
                "workflow_schema_probe": False,
                "probed_workflow_schema": "Extraction",
            }
            available = await runner.available()
            if not available:
                result["profiles"][name].update(
                    ok=False, model_id_available=False, error="configured_model_id_missing"
                )
                result["ok"] = False
                continue
            result["profiles"][name]["model_id_available"] = True
            await runner.run(_Probe, 'Return {"answer":"ok","value":4}．', stage="doctor_probe")
            result["profiles"][name]["basic_output_probe"] = True
            # A small scalar schema can work while the real nested schema fails in the server's
            # JSON grammar compiler．Probe the production contract without inventing evidence．
            await runner.run(
                Extraction,
                'There are no sources．Return {"claims":[],"gaps":["No sources"]}．',
                stage="doctor_workflow_schema_probe",
            )
            result["profiles"][name]["workflow_schema_probe"] = True
            result["profiles"][name].update(
                ok=True,
                model_id_available=True,
                typed_output_probe=True,
                metadata=runner.metadata,
                untested=[
                    "streaming",
                    "server_compute_cancellation",
                    "native tools outside configured output mode",
                ],
            )
        except (ModelError, ConfigurationError, BudgetExceeded, ValueError) as exc:
            result["ok"] = False
            result["profiles"].setdefault(name, {}).update(ok=False, error=str(exc), typed_output_probe=False)
        finally:
            if runner:
                result["profiles"][name]["metadata"] = runner.metadata
                await runner.aclose()
    if output_dir is not None:
        from .store import output_root

        try:
            path = output_root(output_dir)
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
            with TemporaryFile(mode="w+", encoding="utf-8", dir=path) as handle:
                handle.write("Worker doctor UTF-8 書込試験．")
                handle.flush()
                handle.seek(0)
                writable = handle.read() == "Worker doctor UTF-8 書込試験．"
            result["output"] = {"writable": writable, "outside_git": True}
            result["ok"] = result["ok"] and writable
        except (OSError, ValueError) as exc:
            result["output"] = {"writable": False, "error": type(exc).__name__}
            result["ok"] = False
    result["metrics"] = budget.snapshot()
    if search is not None:
        result["metrics"]["search"] = search.metadata
    return result
