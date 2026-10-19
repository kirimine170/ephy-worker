"""Opt-in bounded public smoke using production make_executor without Discord login．"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from urllib.parse import urlsplit

from ephy_worker import answer_routing
from ephy_worker.answer_cache import AnswerCache, positive_facts
from ephy_worker.answer_routing import cache_namespace, request_context
from ephy_worker.budget import Budget, BudgetExceeded
from ephy_worker.config import AnswerPolicy, load_config
from ephy_worker.discord_adapter import DiscordSettings, Request
from ephy_worker.discord_cli import make_executor
from ephy_worker.schema import Report, utc_now
from ephy_worker.store import output_root


def isolate_search_state(config, state: Path):
    search = config.search
    if search is None or search.request_policy is None:
        return config
    policy = search.request_policy.model_copy(update={"sqlite_path": str(state / "search-cache.sqlite3")})
    return config.model_copy(update={"search": search.model_copy(update={"request_policy": policy})})


async def run(args):
    from datetime import timedelta

    from ephy_worker.answer_cache import timestamp

    root = output_root(args.output)
    state = output_root(args.isolated_state or root / "state")
    if root.exists() or state.exists():
        raise ValueError("smoke_requires_fresh_isolated_output_and_state")
    root.mkdir(parents=True)
    config = isolate_search_state(load_config(args.config), state)
    if args.profile not in config.model_profiles or config.search is None:
        raise ValueError("smoke_profile_or_search_missing")
    policy = config.answer_policy or AnswerPolicy(enabled=True)
    config = config.model_copy(update={"answer_policy": policy.model_copy(update={"enabled": True}),
        "limits": config.limits.model_copy(update={"job_seconds": min(300, config.limits.job_seconds),
            "model_requests": min(args.max_model_requests, config.limits.model_requests),
            "max_queries": min(args.max_external_queries, config.limits.max_queries)})})
    settings = DiscordSettings(application_id=100, guild_id=200, allowed_channel_ids=[300], timeout_seconds=300,
        allowed_user_ids=[400, 401], worker_config=args.config, profile=args.profile,
        output_dir=root / "jobs", state_dir=state)
    aggregate = {"physical_model_requests": 0, "search_requests": 0, "unique_external_queries": 0}
    query_keys = set()
    class SmokeBudget(Budget):
        def take(self, category, amount=1):
            if category == "model_requests" and aggregate["physical_model_requests"] + amount > args.max_model_requests:
                raise BudgetExceeded("smoke_total_model_limit")
            super().take(category, amount)
            if category == "model_requests": aggregate["physical_model_requests"] += amount
            if category == "search_requests": aggregate["search_requests"] += amount
        def query(self, query):
            key = " ".join(query.casefold().split())
            if key not in query_keys and len(query_keys) >= args.max_external_queries:
                raise BudgetExceeded("smoke_unique_query_limit")
            result = super().query(query)
            if result:
                query_keys.add(key)
                aggregate["unique_external_queries"] = len(query_keys)
            return result
    answer_routing.Budget = SmokeBudget
    execute = make_executor(settings, config)
    request = Request(500, 100, 200, 300, 400)
    async def ask(question):
        token = request_context.set(request)
        try:
            answer = await execute(question)
        finally:
            request_context.reset(token)
        report = Report.model_validate_json((settings.output_dir / answer.job_id / "report.json").read_bytes())
        return answer, report
    checks, jobs = {}, []
    try:
        _, knowledge = await ask("DNSとは何ですか．一般的な仕組みを短く説明してください．")
        jobs.append(knowledge.job_id)
        route = knowledge.metrics["answer_routing"]
        checks["knowledge"] = route["route"] == "knowledge" and not route["externally_verified"] and not knowledge.claims
        question = "Pythonのlistとtupleの主な違いを，公式資料の出典付きで短く説明してください．"
        _, research = await ask(question)
        jobs.append(research.job_id)
        facts, code = positive_facts(research)
        official = any(urlsplit(s.final_url or s.requested_url).hostname == "docs.python.org" and s.read_passage_ids for s in research.sources)
        checks["research"] = research.state == "completed" and research.answerability == "sufficient" and bool(facts) and code == "ok" and official
        if not checks["research"] or research.metrics["answer_routing"].get("cache_store") != "stored":
            checks["repeat_cache_zero_calls"] = False
            return 2
        before = dict(aggregate)
        _, repeat = await ask(question)
        jobs.append(repeat.job_id)
        checks["repeat_cache_zero_calls"] = repeat.metrics["answer_routing"]["route"] == "cache" and before == aggregate
        checks["original_source_times"] = all(
            any(s.source_id == old.source_id and s.fetched_at == old.fetched_at for old in research.sources)
            for s in repeat.sources)
        cache = AnswerCache(state, config.answer_policy)
        other = cache_namespace(settings, config, Request(501, 100, 200, 300, 401))
        checks["cross_user_miss"] = cache.lookup(question, other)[0] is None
        original_scope = cache_namespace(settings, config, request)
        later = (timestamp(utc_now()) + timedelta(seconds=config.answer_policy.cache_ttl_seconds + 1)).isoformat()
        checks["expired_miss"] = cache.lookup(question, original_scope, now=later)[0] is None
    finally:
        required = {"knowledge", "research", "repeat_cache_zero_calls", "original_source_times", "cross_user_miss", "expired_miss"}
        result = {"mode": "opt_in_public_live_smoke", "checks": checks, "jobs": jobs, "aggregate": aggregate,
                  "limits": {"model_requests": args.max_model_requests, "unique_queries": args.max_external_queries},
                  "passed": set(checks) == required and all(checks.values()), "no_discord_login_or_posts": True}
        (root / "smoke-results.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(result, ensure_ascii=False))
    return 0 if result["passed"] else 2


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True, help="Worker YAML；credentials are environment-only")
    parser.add_argument("--profile", required=True)
    parser.add_argument("--output", type=Path, required=True, help="Fresh directory outside Git")
    parser.add_argument("--isolated-state", type=Path, help="Fresh cache directory；defaults to OUTPUT/state")
    parser.add_argument("--max-model-requests", type=int, choices=range(1, 25), default=24)
    parser.add_argument("--max-external-queries", type=int, choices=range(1, 9), default=8)
    args = parser.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
