"""Portable CLI entrypoint．Exit 0 complete，2 partial，1 failure，130 cancelled．"""

from __future__ import annotations

import argparse
import asyncio
import json
import signal
import sys
from pathlib import Path


class CLIError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="ephy-worker", description="調査・収集・coding Jobを実行するWorker")
    sub = root.add_subparsers(dest="command", required=True)
    doctor = sub.add_parser("doctor", help="設定・接続・型付き出力を確認")
    doctor.add_argument("--config", required=True, type=Path)
    doctor.add_argument("--profile", help="未指定なら全profileを診断")
    doctor.add_argument("--output-dir", type=Path, help="Git外の成果物rootを検査")
    doctor.add_argument(
        "--usage-only", action="store_true", help="Tavily使用量だけを確認し，検索・モデル呼出しを省略"
    )
    research = sub.add_parser("research", help="新規Jobとして調査を実行")
    research.add_argument("--config", required=True, type=Path)
    research.add_argument("--profile", required=True)
    research.add_argument("--question", required=True)
    research.add_argument("--output-dir", required=True, type=Path)
    research.add_argument(
        "--mode",
        choices=("local", "remote"),
        default="local",
        help="local: 検索・取得をこのhostで実行（既定）／remote: manager経由でworkerへ収集を委譲",
    )
    research.add_argument(
        "--source-url", action="append", default=[], help="明示的な追加公開資料URL（複数可）"
    )
    worker = sub.add_parser("worker", help="Workerサービス（遠隔hostでcollect jobを実行）")
    worker_sub = worker.add_subparsers(dest="worker_command", required=True)
    worker_run = worker_sub.add_parser("run", help="workerを起動し，job claim・実行を繰り返す")
    worker_run.add_argument("--config", required=True, type=Path)
    manager = sub.add_parser("manager", help="Managerサービス（job queue）")
    manager_sub = manager.add_subparsers(dest="manager_command", required=True)
    manager_run = manager_sub.add_parser("run", help="managerを起動し，job queueを提供する")
    manager_run.add_argument("--config", required=True, type=Path)
    manager_workers = manager_sub.add_parser("workers", help="登録worker一覧を表示する")
    manager_workers.add_argument("--config", required=True, type=Path)
    job = sub.add_parser("job", help="remote収集jobを操作する")
    job_sub = job.add_subparsers(dest="job_command", required=True)
    job_submit = job_sub.add_parser("submit", help="JSON契約ファイルからjobを投入する")
    job_submit.add_argument("--config", required=True, type=Path)
    job_submit.add_argument("--file", required=True, type=Path)
    for name in ("status", "cancel", "result", "retry", "delete"):
        command = job_sub.add_parser(name)
        command.add_argument("--config", required=True, type=Path)
        command.add_argument("--job-id", required=True)
        if name == "retry":
            command.add_argument("--submit-key")
    coding = sub.add_parser("coding", help="隔離worktreeでcoding Jobを実行")
    coding_sub = coding.add_subparsers(dest="coding_command", required=True)
    coding_run = coding_sub.add_parser("run", help="JSON coding Jobを実行")
    coding_run.add_argument("job_file", type=Path)
    coding_run.add_argument("--profiles", type=Path, help="coding model profile YAML")
    coding_run.add_argument("--output-dir", type=Path, help="Git外のevaluation artifact root")
    coding_run.add_argument("--dry-run", action="store_true", help="解決・実行計画だけを検証")
    coding_verify = coding_sub.add_parser("verify-source", help="候補patchのsource snapshotと現在のcheckoutを照合")
    coding_verify.add_argument("artifact_dir", type=Path)
    coding_verify.add_argument("--repository", required=True, type=Path)
    evaluation = sub.add_parser("eval", help="coding model evaluationを実行")
    evaluation_sub = evaluation.add_subparsers(dest="eval_command", required=True)
    evaluation_run = evaluation_sub.add_parser("run", help="synthetic suiteを実行")
    evaluation_run.add_argument("--suite", default="smoke")
    evaluation_run.add_argument("--model", required=True)
    evaluation_run.add_argument("--repeat", type=int, default=1)
    evaluation_run.add_argument("--profiles", type=Path, help="coding model profile YAML")
    evaluation_run.add_argument("--output-dir", type=Path, help="Git外のevaluation artifact root")
    return root


def _build_remote_collector(config):
    """remote mode の collector を構築する．local search credential は解決しない．"""
    from .manager_client import ManagerClient
    from .remote_collector import RemoteCollector

    if config.remote is None:
        raise CLIError("remote_mode_requires_remote_section")
    try:
        endpoint = config.remote.endpoint()
    except Exception as exc:
        raise CLIError("invalid_remote_manager_endpoint") from exc
    try:
        credential = config.remote.resolve_credential().get_secret_value()
    except Exception as exc:
        raise CLIError("remote_credential_missing") from exc
    if config.limits.job_seconds <= 30:
        raise CLIError("remote_mode_requires_job_seconds_above_30")
    return RemoteCollector(
        ManagerClient(endpoint, credential),
        limits=config.limits,
        target_worker_id=config.remote.target_worker_id,
        poll_interval=config.remote.poll_interval_seconds,
        max_wait_seconds=config.remote.max_wait_seconds,
        deadline_seconds=min(config.remote.deadline_seconds, float(config.limits.job_seconds) - 30.0),
        endpoint=endpoint,
    )


async def _run_worker(config) -> int:
    from .manager_client import ManagerClient
    from .worker_service import WorkerRunner, WorkerService

    if config.worker_service is None:
        raise CLIError("worker_run_requires_worker_section")
    settings = config.worker_service
    try:
        endpoint = settings.endpoint()
    except Exception as exc:
        raise CLIError("invalid_worker_manager_endpoint") from exc
    try:
        credential = settings.resolve_credential().get_secret_value()
    except Exception as exc:
        raise CLIError("worker_credential_missing") from exc
    service = WorkerService(
        ManagerClient(endpoint, credential),
        settings.worker_id,
        search_config=settings.search,
        heartbeat_interval=settings.heartbeat_interval_seconds,
        poll_interval=settings.poll_interval_seconds,
    )
    # service.run() は finally で client を閉じる．
    return await WorkerRunner(service).run()


async def _run_manager(config) -> int:
    from aiohttp import web

    from .manager import ManagerApp

    if config.manager is None:
        raise CLIError("manager_run_requires_manager_section")
    try:
        from .config import _resolve

        _resolve(None, config.manager.requester_credential_env, "manager requester credential")
        config.manager.resolved_worker_credentials()
    except Exception as exc:
        raise CLIError("manager_credential_missing") from exc
    try:
        app = ManagerApp(config.manager)
    except ValueError as exc:
        raise CLIError("manager_storage_must_be_outside_git") from exc
    runner = web.AppRunner(app.app)
    await runner.setup()
    site = web.TCPSite(runner, config.manager.host, config.manager.port)
    await site.start()
    print(f"manager listening on http://{config.manager.host}:{config.manager.port}", file=sys.stderr, flush=True)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signal_name in ("SIGINT", "SIGTERM"):
        try:
            loop.add_signal_handler(getattr(signal, signal_name), stop.set)
        except (NotImplementedError, AttributeError):
            pass
    await stop.wait()
    await runner.cleanup()
    return 0


def _requester_client(config):
    from .config import _resolve
    from .manager_client import ManagerClient

    if config.remote is not None:
        return ManagerClient(
            config.remote.endpoint(),
            config.remote.resolve_credential().get_secret_value(),
        )
    if config.manager is not None:
        credential = _resolve(
            None,
            config.manager.requester_credential_env,
            "manager requester credential",
        )
        return ManagerClient(
            f"http://{config.manager.host}:{config.manager.port}", credential
        )
    raise CLIError("manager_connection_not_configured")


async def _run_manager_command(config, command: str) -> int:
    client = _requester_client(config)
    try:
        if command != "workers":
            raise CLIError("unknown_manager_command")
        result = await client.list_workers()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    finally:
        await client.aclose()


async def _run_job_command(config, args) -> int:
    client = _requester_client(config)
    try:
        if args.job_command == "submit":
            try:
                payload = json.loads(args.file.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise CLIError("invalid_job_file") from exc
            if not isinstance(payload, dict):
                raise CLIError("invalid_job_file")
            result = await client.submit_job(payload)
        elif args.job_command == "status":
            result = await client.get_job(args.job_id)
        elif args.job_command == "cancel":
            result = await client.cancel_job(args.job_id)
        elif args.job_command == "result":
            result = await client.job_result(args.job_id)
        elif args.job_command == "retry":
            result = await client.retry_job(args.job_id, args.submit_key)
        elif args.job_command == "delete":
            result = await client.delete_job(args.job_id)
        else:
            raise CLIError("unknown_job_command")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    finally:
        await client.aclose()


async def dispatch(args) -> int:
    if args.command == "coding":
        from .coding_cli import run_coding_command, run_coding_verify_command

        if args.coding_command == "verify-source":
            return run_coding_verify_command(args)
        return await run_coding_command(args)
    if args.command == "eval":
        from .coding_cli import run_evaluation_command

        return await run_evaluation_command(args)
    from .config import load_config

    config = load_config(args.config)
    if args.command == "doctor":
        if args.profile and args.profile not in config.model_profiles:
            raise CLIError("unknown_model_profile")
        from .diagnostics import doctor

        result = await doctor(
            config, profile_id=args.profile, output_dir=args.output_dir, usage_only=args.usage_only
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["ok"] else 1
    if args.command == "worker":
        return await _run_worker(config)
    if args.command == "manager":
        if args.manager_command == "run":
            return await _run_manager(config)
        return await _run_manager_command(config, args.manager_command)
    if args.command == "job":
        return await _run_job_command(config, args)
    if args.profile not in config.model_profiles:
        raise CLIError("unknown_model_profile")
    if not args.question.strip() or len(args.question) > 8000:
        raise CLIError("question_length_must_be_1_to_8000")
    if len(args.source_url) > config.limits.initial_sources:
        raise CLIError("too_many_supplemental_urls")
    from .search import validate_public_text

    validate_public_text(args.question)
    config.model_profiles[args.profile].resolve()
    if args.mode == "remote":
        collector = _build_remote_collector(config)
    else:
        if config.search is None:
            raise CLIError("local_mode_requires_search_section")
        # Resolve before allocating a job so missing configuration is explicit and creates no phantom run.
        config.search.endpoint()
        config.search.resolve_api_key()
        collector = None
    from .research import ResearchExecutor
    from .store import JobStore

    try:
        store = JobStore(args.output_dir)
    except ValueError as exc:
        raise CLIError("output_dir_must_be_outside_git") from exc
    print(f"Job: {store.job_id}\nArtifacts: {store.directory}", file=sys.stderr, flush=True)
    worker = ResearchExecutor(config, args.profile, store, collector=collector)
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


if __name__ == "__main__":
    sys.exit(main())
