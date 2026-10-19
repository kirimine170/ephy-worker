"""CLI for explicit Sheets diagnostics and private JSON publication．"""

from __future__ import annotations

import json
import signal
from pathlib import Path

from .google_sheets import SheetsAdapter, SheetsError, output_path, publish_json, resolve_source


def add_parser(sub) -> None:
    sheets = sub.add_parser("google-sheets", help="許可されたGoogle Sheetsの読み取り専用取得")
    commands = sheets.add_subparsers(dest="sheets_command", required=True)
    for name in ("doctor", "list-tabs", "read"):
        command = commands.add_parser(name)
        command.add_argument("--config", required=True, type=Path)
        command.add_argument("--source", required=name != "doctor", help="設定内のsource alias")
        if name == "read":
            command.add_argument("--tab", required=True)
            command.add_argument("--range", required=True)
            command.add_argument("--output", required=True, type=Path)


def run(config, args) -> int:
    if config.google_sheets is None:
        raise SheetsError("google_sheets_configuration_missing")
    settings = config.google_sheets
    sources = [args.source] if args.source else list(settings.sources)
    # Refuse all unallowlisted sources before loading any credential or sending requests．
    for source in sources:
        resolve_source(settings, source)
    if args.sheets_command == "read":
        output_path(args.output)
    adapter = SheetsAdapter(settings)
    previous_term = signal.getsignal(signal.SIGTERM)

    def cancelled(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, cancelled)
    try:
        results = []
        for source in sources:
            result = adapter.execute(
                source, tab=args.tab if args.sheets_command == "read" else None,
                cell_range=args.range if args.sheets_command == "read" else None,
            )
            if args.sheets_command == "read":
                publish_json(args.output, result)
                # Do not echo cell content, titles or local output paths to console logs．
                print(json.dumps({"status": result.status, "diagnostic": result.diagnostic,
                                  "requests_used": result.requests_used}))
            else:
                results.append(result.model_dump(mode="json"))
        if args.sheets_command != "read":
            print(json.dumps(results, ensure_ascii=False, indent=2))
        return 0 if (result.status == "ok" if args.sheets_command == "read"
                     else all(entry["status"] == "ok" for entry in results)) else 1
    finally:
        adapter.close()
        signal.signal(signal.SIGTERM, previous_term)
