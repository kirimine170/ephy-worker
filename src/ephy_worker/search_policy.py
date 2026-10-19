"""Optional process-local search pacing，bounded success cache and service cooldown．"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
import weakref
from collections import OrderedDict
from dataclasses import dataclass, field

from .budget import BudgetExceeded
from .schema import Candidate
from .search import SearchError, validate_query

_MAX_BACKENDS = 32
_STATE_ATTRIBUTE = "_ephy_worker_search_policy_states"


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


@dataclass
class _Entry:
    created: float
    expires: float
    data: bytes


@dataclass
class _State:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last_started: float | None = None
    cooldown_until: float = 0
    cooldown_reason: str = ""
    cache: OrderedDict = field(default_factory=OrderedDict)
    cache_bytes: int = 0
    owners: weakref.WeakSet = field(default_factory=weakref.WeakSet)

    def prune(self, now: float, entries: int = 256, size: int = 16 * 1024**2):
        for key in list(self.cache):
            if self.cache[key].expires <= now:
                self.cache_bytes -= len(self.cache.pop(key).data)
        while len(self.cache) > entries or self.cache_bytes > size:
            self.cache_bytes -= len(self.cache.popitem(last=False)[1].data)


class PolicySearchProvider:
    def __init__(self, provider):
        self.provider = provider
        self.config, self.budget = provider.config, provider.budget
        self.policy = self.config.request_policy
        credential = getattr(provider, "_api_key", None)
        self._backend = _digest([
            self.config.provider, provider.base_url, self.config.engine,
            credential.get_secret_value() if credential is not None else None,
        ])
        # Parameters affecting results partition cache，but never service cooldown．
        self._semantic = _digest([self.config.language, self.config.max_results])
        self._state: _State | None = None
        self._loop = None
        self.last_diagnostic: dict = {}
        self._hits = 0
        self._cooldowns = 0

    def _shared(self) -> _State:
        loop = asyncio.get_running_loop()
        if self._loop is not None and self._loop is not loop:
            raise SearchError("search_policy_event_loop_changed")
        if self._state is not None:
            return self._state
        # State lives on the loop itself，so closed-loop locks cannot retain
        # arbitrarily many old loops through a module-global registry．
        states = getattr(loop, _STATE_ATTRIBUTE, None)
        if states is None:
            states = {}
            setattr(loop, _STATE_ATTRIBUTE, states)
        now = time.monotonic()
        for key, state in list(states.items()):
            state.prune(now)
            # Preserve pacing after the previous job closes，for the maximum
            # configurable interval；do not evict a live cooldown to admit a backend．
            if (not state.owners and not state.cache and state.cooldown_until <= now
                    and (state.last_started is None or now - state.last_started >= 300)):
                del states[key]
        if self._backend not in states:
            if len(states) >= _MAX_BACKENDS:
                raise SearchError("search_policy_backend_limit")
            states[self._backend] = _State()
        self._state, self._loop = states[self._backend], loop
        self._state.owners.add(self)
        return self._state

    @property
    def metadata(self) -> dict:
        state = self._state
        return {**self.provider.metadata, "request_policy": {
            **self.policy.model_dump(), "pacing_unit": "logical_query",
            "cache_hits": self._hits, "cooldown_rejections": self._cooldowns,
            "cooldown_remaining_seconds": round(max(0, state.cooldown_until - time.monotonic()), 3)
            if state else 0,
        }}

    def _check_cooldown(self, state: _State):
        remaining = state.cooldown_until - time.monotonic()
        if remaining > 0:
            self._cooldowns += 1
            self.last_diagnostic = {"engine": self.config.engine, "code": "search_provider_cooldown",
                                    "reasons": [state.cooldown_reason],
                                    "cooldown_remaining_seconds": round(remaining, 3)}
            raise SearchError("search_provider_cooldown", retry_after=remaining)

    async def search(self, query: str, query_id: str) -> list[Candidate]:
        query = validate_query(query)  # Also required before cache hits．
        self.budget.check()
        state = self._shared()
        self._check_cooldown(state)
        try:
            async with asyncio.timeout(self.budget.remaining_seconds):
                async with state.lock:
                    self.budget.check()
                    self._check_cooldown(state)
                    now = time.monotonic()
                    state.prune(now, self.policy.cache_max_entries, self.policy.cache_max_bytes)
                    key = (self._semantic, _digest(query))
                    entry = state.cache.get(key)
                    if (entry is not None and self.policy.cache_ttl_seconds > 0
                            and now - entry.created < self.policy.cache_ttl_seconds):
                        state.cache.move_to_end(key)
                        self._hits += 1
                        candidates = [Candidate.model_validate(item) for item in json.loads(entry.data)]
                        for candidate in candidates:
                            candidate.query_id = query_id
                        self.last_diagnostic = {"engine": self.config.engine, "result_count": len(candidates),
                                                "code": "search_cache_hit"}
                        return candidates
                    if state.last_started is not None:
                        delay = self.policy.min_interval_seconds - (now - state.last_started)
                        if delay > 0:
                            await asyncio.sleep(delay)
                    self.budget.check()
                    state.last_started = time.monotonic()
                    try:
                        candidates = await self.provider.search(query, query_id)
                    except SearchError as exc:
                        self.last_diagnostic = {**self.provider.last_diagnostic, "code": exc.code}
                        reasons = self.last_diagnostic.get("reasons", [])
                        challenge = "challenge" in exc.code or "captcha" in reasons
                        if exc.status == 429 or challenge or "too many requests" in reasons:
                            delay = max(self.policy.cooldown_seconds, exc.retry_after or 0)
                            state.cooldown_until = max(state.cooldown_until, time.monotonic() + delay)
                            state.cooldown_reason = "challenge" if challenge else "rate_limited"
                        raise
                    self.last_diagnostic = dict(self.provider.last_diagnostic)
                    self.budget.check()
                    if self.policy.cache_ttl_seconds > 0:
                        data = json.dumps([item.model_dump() for item in candidates], ensure_ascii=False).encode()
                        if len(data) <= self.policy.cache_max_bytes:
                            if key in state.cache:
                                state.cache_bytes -= len(state.cache.pop(key).data)
                            now = time.monotonic()
                            state.cache[key] = _Entry(now, now + self.policy.cache_ttl_seconds, data)
                            state.cache_bytes += len(data)
                            state.prune(now, self.policy.cache_max_entries, self.policy.cache_max_bytes)
                    return candidates
        except TimeoutError:
            raise BudgetExceeded("job_timeout") from None

    async def engine_health(self) -> dict:
        self.budget.check()
        result = await self.provider.engine_health()
        self.last_diagnostic = dict(self.provider.last_diagnostic)
        return result

    async def aclose(self) -> None:
        try:
            await self.provider.aclose()
        finally:
            if self._state is not None:
                self._state.owners.discard(self)
