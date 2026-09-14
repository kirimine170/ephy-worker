"""Portable CLI entrypoint．Exit 0 complete，2 partial，1 failure，130 cancelled．"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path


class CLIError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="ephy-worker", description="公開資料の本文を取得・照合するWorker")
    sub = root.add_subparsers(dest="command", required=True)
    doctor = sub.add_parser("doctor", help="設定・接続・型付き出力を確認")
    doctor.add_argument("--config", required=True, type=Path)
    doctor.add_argument("--profile", help="未指定なら全profileを診断")
    doctor.add_argument("--output-dir", type=Path, help="Git外の成果物rootを検査")
    research = sub.add_parser("research", help="新規Jobとして調査を実行")
    research.add_argument("--config", required=True, type=Path)
    research.add_argument("--profile", required=True)
    research.add_argument("--question", required=True)
    research.add_argument("--output-dir", required=True, type=Path)
    research.add_argument(
        "--source-url", action="append", default=[], help="明示的な追加公開資料URL（複数可）"
    )
    return root


async def dispatch(args) -> int:
    from .config import load_config

    config = load_config(args.config)
    if args.profile and args.profile not in config.model_profiles:
        raise CLIError("unknown_model_profile")
    if args.command == "doctor":
        from .diagnostics import doctor

        result = await doctor(config, profile_id=args.profile, output_dir=args.output_dir)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["ok"] else 1
    if not args.question.strip() or len(args.question) > 8000:
        raise CLIError("question_length_must_be_1_to_8000")
    if len(args.source_url) > config.limits.initial_sources:
        raise CLIError("too_many_supplemental_urls")
    from .search import validate_public_text

    validate_public_text(args.question)
    from .research import ResearchExecutor
    from .store import JobStore

    # Resolve before allocating a job so missing configuration is explicit and creates no phantom run．
    config.model_profiles[args.profile].resolve()
    config.search.endpoint()
    config.search.resolve_api_key()
    try:
        store = JobStore(args.output_dir)
    except ValueError as exc:
        raise CLIError("output_dir_must_be_outside_git") from exc
    print(f"Job: {store.job_id}\nArtifacts: {store.directory}", file=sys.stderr, flush=True)
    worker = ResearchExecutor(config, args.profile, store)
    report = await worker.run(args.question, args.source_url)
    print(
        json.dumps(
            {
                "job_id": report.job_id,
                "state": report.state,
                "stop_reason": report.stop_reason,
                "report": str(store.directory / "report.json"),
            },
            ensure_ascii=False,
        )
    )
    return {"completed": 0, "partial": 2, "failed": 1, "cancelled": 130}[report.state]


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    args = parser().parse_args(argv)
    try:
        return asyncio.run(dispatch(args))
    except KeyboardInterrupt:
        print("cancelled", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001 — CLI errors must not expose credentials．
        # Exception bodies can contain HTTP response text，headers or credential values．
        from .config import ConfigurationError

        detail = {"error": type(exc).__name__, "code": getattr(exc, "code", "configuration_or_io_error")}
        if isinstance(exc, ConfigurationError):
            detail["detail"] = str(exc)
        print(
            json.dumps(
                detail,
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 1
