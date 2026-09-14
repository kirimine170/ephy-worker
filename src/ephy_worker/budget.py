"""All actual requests and retries share one monotonic budget."""

from __future__ import annotations

import time
from collections import Counter

from .schema import Limits


class BudgetExceeded(RuntimeError):
    pass


class Budget:
    def __init__(self, limits: Limits):
        self.limits = limits
        self.started = time.monotonic()
        self.counts: Counter = Counter()
        self.query_keys: set[str] = set()
        self.source_keys: set[str] = set()
        self.input_tokens: int | None = None
        self.output_tokens: int | None = None
        self.stage_seconds: Counter = Counter()

    @property
    def remaining_seconds(self) -> float:
        return max(0, self.limits.job_seconds - (time.monotonic() - self.started))

    def check(self) -> None:
        if not self.remaining_seconds:
            raise BudgetExceeded("job_timeout")

    def take(self, category: str, amount: int = 1) -> None:
        self.check()
        limit = getattr(self.limits, category)
        if self.counts[category] + amount > limit:
            raise BudgetExceeded(category)
        self.counts[category] += amount

    def query(self, query: str) -> bool:
        key = " ".join(query.casefold().split())
        if key in self.query_keys:
            return False
        self.take("max_queries")
        self.query_keys.add(key)
        return True

    def source(self, url: str) -> bool:
        if url in self.source_keys:
            return False
        self.take("max_sources")
        self.source_keys.add(url)
        return True

    def snapshot(self) -> dict:
        return {
            "requests": {k: self.counts[k] for k in ("search_requests", "fetch_requests", "model_requests")},
            "counts": dict(self.counts),
            "elapsed_seconds": round(time.monotonic() - self.started, 3),
            "stage_seconds": dict(self.stage_seconds),
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "limits": self.limits.model_dump(),
        }
