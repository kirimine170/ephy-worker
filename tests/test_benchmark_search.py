import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from ephy_worker.config import SearchConfig
from ephy_worker.schema import Limits
from ephy_worker.search import SearchError

spec = importlib.util.spec_from_file_location("benchmark_search", Path(__file__).parents[1] / "scripts/benchmark_search.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_public_queries_validate_before_network(tmp_path):
    path = tmp_path / "queries.json"
    row = {"id": "ja", "query": "Python documentation", "language": "ja", "region": "jp-jp"}
    path.write_text(json.dumps([row]), encoding="utf-8")
    rows, digest = module.public_queries(path)
    assert rows == [row] and len(digest) == 64
    row["query"] = "api_key=private"
    path.write_text(json.dumps([row]), encoding="utf-8")
    with pytest.raises(ValueError):
        module.public_queries(path)


async def test_refusal_stops_backend_preserves_denominator(monkeypatch):
    class Provider:
        def __init__(self, budget):
            self.budget = budget
            self.last_diagnostic = {"code": "ddgs_rate_limited"}

        async def search(self, query, query_id):
            self.budget.take("search_requests")
            raise SearchError("ddgs_rate_limited", status=429, retry_after=600)

        async def aclose(self):
            pass

    def factory(config, budget):
        assert config.request_policy.cache_ttl_seconds == 0 and config.request_policy.sqlite_path is None
        assert config.retries == 0
        return Provider(budget)

    monkeypatch.setattr(module, "create_search_provider", factory)
    config = SimpleNamespace(search=SearchConfig(provider="ddgs", backend="brave", region="us-en"), limits=Limits())
    rows = [{"id": str(n), "query": "public query", "language": "en", "region": "us-en"} for n in range(4)]
    result = await module.benchmark(config, rows, 0)
    assert result["attempted"] == 1 and result["errors"] == 1 and result["skipped"] == 3
    assert result["success_rate_of_attempted"] == 0 and result["rows"][0]["actual_requests"] == 1
    assert all(row["status"] == "skipped" for row in result["rows"][1:])
