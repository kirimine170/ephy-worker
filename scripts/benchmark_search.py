"""Explicit live public-query benchmark；no cache，fallback or retries．"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import time
from pathlib import Path

from ephy_worker.budget import Budget, BudgetExceeded
from ephy_worker.config import SearchRequestPolicy, load_config
from ephy_worker.search import SearchError, create_search_provider, validate_query
from ephy_worker.store import output_root


def public_queries(path):
    data = Path(path).read_bytes()
    if len(data) > 128 * 1024:
        raise ValueError("query file too large")
    rows = json.loads(data)
    if not isinstance(rows, list) or not 1 <= len(rows) <= 100:
        raise ValueError("expected 1–100 public queries")
    identifiers = set()
    for row in rows:
        if (not isinstance(row, dict) or set(row) != {"id", "query", "language", "region"}
                or not isinstance(row["id"], str) or row["id"] in identifiers
                or row["language"] not in {"ja", "en"} or row["region"] not in {"jp-jp", "us-en"}):
            raise ValueError("invalid public query row")
        identifiers.add(row["id"])
        validate_query(row["query"])
    return rows, hashlib.sha256(data).hexdigest()


async def benchmark(config, queries, interval=3):
    rows, stop, started = [], None, None
    for row in queries:
        result = {**row, "provider": config.search.provider, "backend": config.search.backend or config.search.engine,
                  "cache": "disabled", "actual_requests": 0, "latency_seconds": 0, "urls": []}
        if stop:
            result.update(status="skipped", error=stop)
            rows.append(result)
            continue
        if started is not None:
            await asyncio.sleep(max(0, interval - (time.monotonic() - started)))
        started = time.monotonic()
        search_config = config.search.model_copy(deep=True)
        if search_config.provider == "ddgs":
            search_config.region = row["region"]
        # Policy still provides pacing/cooldown，but never consults any cache DB．
        search_config.request_policy = SearchRequestPolicy(min_interval_seconds=0, cache_ttl_seconds=0)
        search_config.retries = 0
        budget = Budget(config.limits)
        provider = create_search_provider(search_config, budget)
        try:
            candidates = await provider.search(row["query"], row["id"])
            result.update(status="success", result_count=len(candidates), urls=[item.url for item in candidates])
        except (SearchError, BudgetExceeded) as exc:
            code = getattr(exc, "code", str(exc))
            result.update(status="error", error=code, http_status=getattr(exc, "status", None))
            if ("challenge" in code or getattr(exc, "status", None) in {403, 429}
                    or code == "search_provider_cooldown"):
                stop = code
        finally:
            await provider.aclose()
            result.update(actual_requests=budget.counts["search_requests"],
                          latency_seconds=round(time.monotonic() - started, 3), diagnostic=provider.last_diagnostic)
        rows.append(result)
    attempted = sum(row["status"] != "skipped" for row in rows)
    successes = sum(row["status"] == "success" for row in rows)
    return {"planned": len(rows), "attempted": attempted, "successes": successes,
            "errors": attempted - successes, "skipped": len(rows) - attempted,
            "success_rate_of_attempted": successes / attempted if attempted else None,
            "cache": "disabled", "fallback": False, "rows": rows}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--queries", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--limit", type=int, default=20, choices=range(1, 101))
    parser.add_argument("--interval", type=float, default=3)
    args = parser.parse_args(argv)
    if not 3 <= args.interval <= 300:
        parser.error("interval must be 3–300 seconds")
    config = load_config(args.config)
    if config.search is None:
        parser.error("search config required")
    rows, query_hash = public_queries(args.queries)
    target = output_root(args.output)
    if target.exists():
        parser.error("output already exists")
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    result = asyncio.run(benchmark(config, rows[:args.limit], args.interval))
    result["query_file_sha256"] = query_hash
    with target.open("x", encoding="utf-8") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2, allow_nan=False)
    print(json.dumps({key: result[key] for key in ("planned", "attempted", "successes", "errors", "skipped")}))


if __name__ == "__main__":
    main()
