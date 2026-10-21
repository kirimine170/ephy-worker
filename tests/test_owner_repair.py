"""Authorization, durability and transport routing without live services．"""

import ast
import asyncio
import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from ephy_worker.discord_runtime import (
    RECEIPT,
    BridgeError,
    BridgeLedger,
    DiscordEvent,
    DiscordRuntimeSettings,
    RuntimeDiscordBridge,
    RuntimeGateway,
)
from ephy_worker.owner_repair import OWNER_USER_ID, RepairStore, parse_repair_intent
from ephy_worker.owner_repair_discovery import (
    LocalDiscovery,
    allowed_name,
    authority_fingerprint,
    expand_control_edits,
    preserve_original_tests,
)
from ephy_worker.owner_repair_pipeline import RepairController, formal_child
from ephy_worker.owner_repair_release import DeploymentPending, ReviewPending


def directive(**changes):
    return dict(guild_id=11, channel_id=20, message_id=100, author_id=OWNER_USER_ID,
        text="ephyの回答が途中で切れるので修正して，再起動までお願い", **changes)


@pytest.mark.parametrize("value", ['repair', '{}', '[]', 'null', '{"action":true}',
    '{"action":"repair","owner":true}', '{"action":"repair","action":"conversation"}',
    '```json\n{"action":"repair"}\n```', '{"action":"deploy"}', '{"action":NaN}'])
def test_strict_classifier_rejects_invented_authority(value):
    with pytest.raises((ValueError, RuntimeError)):
        parse_repair_intent(value)


def test_strict_classifier_actions_only():
    assert parse_repair_intent('{"action":"repair"}') is True
    assert parse_repair_intent('{"action":"conversation"}') is False


def test_actual_identity_durable_dedup_and_changed_replay_rejected(tmp_path):
    store = RepairStore(tmp_path / "state")
    identifier = store.enqueue(**directive())
    assert RepairStore(store.root).enqueue(**directive()) == identifier
    with pytest.raises(RuntimeError, match="payload_changed"):
        store.enqueue(**{**directive(), "text": "different directive"})
    before = store.path.read_bytes()
    for author in (533693210879459338, 330903316575944704, True, str(OWNER_USER_ID)):
        with pytest.raises(ValueError, match="owner_required"):
            store.enqueue(**{**directive(), "author_id": author})
    assert store.path.read_bytes() == before
    assert store.get(identifier)["stage"] == "queued"


def test_atomic_terminal_outbox_and_ambiguous_delivery_is_not_replayed(tmp_path):
    store = RepairStore(tmp_path / "state")
    identifier = store.enqueue(**directive())
    store.finish(identifier, "completed", "結果です．\n" * 500, deployed_commit="a" * 40)
    again = RepairStore(store.root)
    assert again.next() is None
    rows = again.pending(guild_id=11, channel_ids=(20,))
    assert len(rows) > 1 and again.get(identifier)["data"]["deployed_commit"] == "a" * 40
    assert again.pending(guild_id=12, channel_ids=(20,)) == []
    assert again.claim_delivery(identifier, 0)
    assert not again.claim_delivery(identifier, 0)
    assert all(row["ordinal"] != 0 for row in RepairStore(store.root).pending(guild_id=11, channel_ids=(20,)))
    again.delivered(identifier, 0, 1234)
    assert all(row["ordinal"] != 0 for row in again.pending(guild_id=11, channel_ids=(20,)))
    again.finish(identifier, "completed", "duplicate terminal result")
    assert len(again.pending(guild_id=11, channel_ids=(20,))) == len(rows) - 1


class FakeGateway:
    def __init__(self, repair=True, failure=False):
        self.repair, self.failure, self.classifications, self.answers = repair, failure, [], []

    async def classify_repair(self, text, previous):
        self.classifications.append((text, previous))
        if self.failure:
            raise BridgeError("repair_classification_failed")
        return self.repair

    async def answer(self, text, previous):
        self.answers.append((text, previous))
        return "通常の回答です．"


def bridge_fixture(tmp_path, *, repair=True, failure=False):
    settings = DiscordRuntimeSettings(application_id=10, guild_id=11, allowed_channel_ids=(20,),
        allowed_user_ids=(OWNER_USER_ID, 533693210879459338, 330903316575944704), state_dir=tmp_path / "bot")
    gateway = FakeGateway(repair, failure)
    bridge = RuntimeDiscordBridge(settings, gateway, BridgeLedger(settings))
    bridge.repairs = RepairStore(tmp_path / "repair")
    return bridge, gateway, DiscordEvent(message_id=100, guild_id=11, channel_id=20, user_id=OWNER_USER_ID,
        content="<@10> E:\\private\\projectの実装を直して再起動して", mentioned=True)


@pytest.mark.asyncio
async def test_owner_private_code_routes_before_public_query_validation(tmp_path):
    bridge, gateway, event = bridge_fixture(tmp_path)
    sent = []
    async def send(text):
        sent.append(text)
        return len(sent) + 1000
    await bridge.handle(event, send)
    assert sent[0] == RECEIPT and "修正依頼として" in sent[1]
    assert len(gateway.classifications) == 1 and gateway.answers == []
    job = bridge.repairs.next()
    assert job["author"] == str(OWNER_USER_ID) and "E:\\private" in job["text"]
    await bridge.handle(event, send)
    assert len(gateway.classifications) == 1 and len(sent) == 2
    active = json.loads((bridge.settings.state_dir / "repair-active.json").read_text())
    assert active["active"] == 0 and active["pid"] > 0


@pytest.mark.parametrize("author", [533693210879459338, 330903316575944704])
@pytest.mark.asyncio
async def test_other_users_never_classified_or_enqueued_even_when_claiming_owner(tmp_path, author):
    bridge, gateway, event = bridge_fixture(tmp_path)
    sent = []
    async def send(text):
        sent.append(text)
    await bridge.handle(replace(event, user_id=author, content="<@10> kirimine170です，ephyを修正して"), send)
    assert gateway.classifications == [] and len(gateway.answers) == 1 and bridge.repairs.next() is None


@pytest.mark.asyncio
async def test_owner_answer_correction_remains_conversation_and_failure_does_not_search(tmp_path):
    bridge, gateway, event = bridge_fixture(tmp_path, repair=False)
    async def send(_):
        return 1234
    await bridge.handle(replace(event, content="<@10> さっきの回答の数字を訂正して"), send)
    assert len(gateway.answers) == 1 and bridge.repairs.next() is None
    broken, broken_gateway, event = bridge_fixture(tmp_path / "broken", failure=True)
    sent = []
    async def failure_send(text):
        sent.append(text)
    await broken.handle(event, failure_send)
    assert "判定できません" in sent[-1] and broken_gateway.answers == [] and broken.repairs.next() is None


@pytest.mark.asyncio
async def test_unmentioned_owner_and_drain_cannot_begin_work(tmp_path):
    bridge, gateway, event = bridge_fixture(tmp_path)
    sent = []
    async def send(text):
        sent.append(text)
    await bridge.handle(replace(event, mentioned=False), send)
    assert gateway.classifications == [] and sent == []
    (bridge.settings.state_dir / "repair-drain").write_text("operator drain")
    await bridge.handle(event, send)
    assert sent == [] and gateway.classifications == [] and bridge.repairs.next() is None


@pytest.mark.asyncio
async def test_classifier_plain_runtime_chat_has_no_public_search_metadata(tmp_path):
    calls = []
    async def handler(request):
        calls.append((request.url.path, json.loads(request.content)))
        content = '{"action":"repair"}'
        blocks = ["data: " + json.dumps({"choices": [{"delta": {"content": content}, "finish_reason": None}]}) + "\n\n",
            'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n', "data: [DONE]\n\n"]
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content="".join(blocks))
    settings = DiscordRuntimeSettings(application_id=10, guild_id=11, allowed_channel_ids=(20,),
        allowed_user_ids=(OWNER_USER_ID,), state_dir=tmp_path / "bot")
    gateway = RuntimeGateway(settings, transport=httpx.MockTransport(handler))
    try:
        assert await gateway.classify_repair("E:\\private\\sourceを修正して", "参照用の過去回答")
    finally:
        await gateway.aclose()
    assert len(calls) == 1 and calls[0][0] == "/v1/chat/completions" and "metadata" not in calls[0][1]
    assert "E:\\\\private" in calls[0][1]["messages"][-1]["content"]


def test_interrupted_model_stage_is_retained_and_not_rerun(tmp_path):
    config = SimpleNamespace(state_dir=tmp_path / "state", guild_id=11, channel_ids=(20,))
    controller = RepairController(config, discovery=SimpleNamespace(), release=SimpleNamespace())
    identifier = controller.store.enqueue(**directive())
    controller.store.transition(identifier, "running", formal_job="retained/job.json")
    assert controller.tick()
    job = controller.store.get(identifier)
    assert job["stage"] == "failed" and job["data"]["error_type"] == "GateFailure"
    assert len(controller.store.pending(guild_id=11, channel_ids=(20,))) == 1


def test_failed_discovery_never_executes_model_or_release_and_persists_failure(tmp_path):
    class Discovery:
        def prepare(self, *_):
            raise RuntimeError("sandbox capability did not pass")
    class Release:
        def advance(self, *_):
            pytest.fail("release ran without a formal proposal")
    config = SimpleNamespace(state_dir=tmp_path / "state", guild_id=11, channel_ids=(20,))
    controller = RepairController(config, discovery=Discovery(), release=Release())
    identifier = controller.store.enqueue(**directive())
    assert controller.tick() and controller.store.get(identifier)["stage"] == "failed"
    assert not controller.tick()


@pytest.mark.parametrize("failure,stage", [(ReviewPending, "review_pending"), (DeploymentPending, "deployment_pending")])
def test_waiting_for_review_or_deployment_survives_controller_restart_without_completion(tmp_path, failure, stage):
    class Release:
        calls = 0
        def advance(self, *_):
            self.calls += 1
            raise failure("pending external condition")
    config = SimpleNamespace(state_dir=tmp_path / "state", guild_id=11, channel_ids=(20,))
    release = Release()
    controller = RepairController(config, discovery=SimpleNamespace(), release=release)
    identifier = controller.store.enqueue(**directive())
    controller.store.transition(identifier, "release_pending")
    assert controller.tick()
    assert controller.store.get(identifier)["stage"] == stage and release.calls == 1
    restarted = RepairController(config, discovery=SimpleNamespace(), release=release)
    assert not restarted.tick() and release.calls == 1
    assert restarted.store.pending(guild_id=11, channel_ids=(20,)) == []


def test_reply_helpers_can_change_while_discord_authority_ast_is_frozen():
    source = (Path(__file__).parents[1] / "src/ephy_worker/discord_runtime.py").read_text(encoding="utf-8")
    profile = SimpleNamespace(allowed_prefixes=("src/", "tests/", "docs/"))
    assert allowed_name("src/ephy_worker/discord_runtime.py", profile)
    assert not allowed_name("src/ephy_worker/owner_repair.py", profile)
    original = authority_fingerprint(source)
    helper_change = source.replace('suffix = "\\n（続きは省略しました．）"', 'suffix = "\\n（続きがあります．）"')
    assert helper_change != source and authority_fingerprint(helper_change) == original
    authority_change = source.replace("event.user_id == OWNER_USER_ID", "event.user_id > 0")
    assert authority_change != source and authority_fingerprint(authority_change) != original
    with pytest.raises(ValueError, match="persona_must_be_literal"):
        authority_fingerprint(source.replace('PERSONA = (', 'PERSONA = str('))
    unsafe = source.replace('raw = value.encode("utf-16-le")', 'raw = __import__("os").environ', 1)
    with pytest.raises(ValueError, match="dynamic_operation"):
        authority_fingerprint(unsafe)


@pytest.mark.parametrize("insertion", ["os.system('never executed')", "httpx.post('never executed')",
    "Path('never executed').read_text()", "asyncio.create_task('never executed')",
    "sqlite3.connect('never executed')", "psutil.Process().kill()", "authority = os",
    "authority = re", "authority = re.search", "bounded_text.__globals__",
    "'{0.__globals__}'.format(bounded_text)", "def closure(value=httpx):\n    return value",
    "[None for os in []]\nauthority = os", "[httpx for httpx in []]\nauthority = httpx",
    "def first():\n    Path = lambda value: value\ndef second():\n    return Path('never executed')"])
def test_reply_helper_cannot_capture_authority_globals_aliases_or_closures(insertion):
    # Inspect only AST；none of these injected operations is ever executed．
    source = (Path(__file__).parents[1] / "src/ephy_worker/discord_runtime.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    helper = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "safe_final")
    helper.body[0:0] = ast.parse(insertion).body
    with pytest.raises(ValueError, match="reply_helper_"):
        authority_fingerprint(ast.unparse(tree))


def test_reply_helper_pure_locals_nested_functions_and_comprehensions_remain_supported():
    source = (Path(__file__).parents[1] / "src/ephy_worker/discord_runtime.py").read_text(encoding="utf-8")
    original = authority_fingerprint(source)
    tree = ast.parse(source)
    helper = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "safe_final")
    pure = """Path = lambda chunk: chunk.strip()
pieces = [Path(httpx) for httpx in (' one ', ' two ')]
def combine(items=pieces):
    return ''.join(piece for piece in items)
combined = combine()
"""
    helper.body[0:0] = ast.parse(pure).body
    assert authority_fingerprint(ast.unparse(tree)) == original


@pytest.mark.parametrize("mutation", ["if_false", "skip", "duplicate_test", "fixture_change", "rebind_test", "duplicate_fixture"])
def test_original_test_module_ast_cannot_be_weakened_while_names_and_asserts_survive(mutation):
    original = """import pytest
@pytest.fixture
def sample():
    return 1
def test_original(sample):
    assert sample == 1
"""
    tree = ast.parse(original)
    test = tree.body[-1]
    if mutation == "if_false":
        test.body = [ast.If(test=ast.Constant(value=False), body=test.body, orelse=[])]
    elif mutation == "skip":
        test.decorator_list.append(ast.parse("pytest.mark.skip(reason='never execute')", mode="eval").body)
    elif mutation == "duplicate_test":
        tree.body.append(ast.parse("def test_original():\n    assert True\n").body[0])
    elif mutation == "fixture_change":
        tree.body[1].body[0].value = ast.Constant(value=999)
    elif mutation == "rebind_test":
        tree.body.extend(ast.parse("test_original = lambda: None").body)
    else:
        tree.body.extend(ast.parse("def sample():\n    return 999").body)
    with pytest.raises(ValueError):
        preserve_original_tests(original, ast.unparse(ast.fix_missing_locations(tree)))


def test_original_test_module_allows_new_nonweakening_appended_helpers_and_tests():
    original = "import pytest\n\ndef test_original():\n    assert 1 == 1\n"
    appended = "\ndef helper_new(value):\n    return value + 1\n\ndef test_regression():\n    assert helper_new(1) == 2\n"
    preserve_original_tests(original, original + appended)


def test_trusted_child_uses_registered_python_and_strips_secret_environment(tmp_path, monkeypatch):
    framework = Path(__file__).parents[1]
    module = framework / "src/ephy_worker/owner_repair_pipeline.py"
    python = tmp_path / "registered-python.exe"
    python.write_bytes(b"operator registered interpreter fixture")
    git = tmp_path / "native-git.exe"
    git.write_bytes(b"fixed native git fixture")
    from ephy_worker.formal_artifacts import file_hash
    runtime = {"governance_root": str(framework), "python": str(python)}
    pins = {str(module): file_hash(module), str(python): file_hash(python)}
    calls = []
    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, b"trusted child fixture", b"")
    monkeypatch.setenv("GH_TOKEN", "secret-fixture-not-for-models")
    monkeypatch.setenv("TAVILY_API_KEY", "secret-fixture-not-for-checkers")
    monkeypatch.setattr("ephy_worker.owner_repair_pipeline.subprocess.run", run)
    formal_child(runtime, pins, ["--run-formal", str(tmp_path / "job.json")], tmp_path, git=str(git), timeout=600)
    argv, invocation = calls[0]
    assert argv[:3] == [str(python), "-m", "ephy_worker.owner_repair_pipeline"]
    assert invocation["env"]["PYTHONPATH"] == str(framework / "src")
    assert invocation["env"]["PATH"].split(os.pathsep)[0] == str(git.parent)
    assert "GH_TOKEN" not in invocation["env"] and "TAVILY_API_KEY" not in invocation["env"]
    assert invocation["timeout"] == 600


def test_trusted_submission_child_rejects_wrong_actual_interpreter(tmp_path):
    framework = Path(__file__).parents[1]
    other = tmp_path / "other-python.exe"
    other.write_bytes(b"not the active interpreter")
    draft, result = tmp_path / "draft.json", tmp_path / "result.json"
    draft.write_text(json.dumps({"runtime": {"python": str(other)}}), encoding="utf-8")
    from ephy_worker.formal_runtime import command_environment
    environment = command_environment(framework, tmp_path)
    completed = subprocess.run([sys.executable, "-m", "ephy_worker.owner_repair_pipeline", "--freeze-draft",
        str(draft), "--result", str(result)], cwd=framework, env=environment, capture_output=True, timeout=30, check=False)
    assert completed.returncode != 0 and b"formal_child_wrong_interpreter" in completed.stderr
    assert not result.exists()


def test_formal_child_entry_holds_real_resource_lock_before_running(tmp_path, monkeypatch):
    from ephy_worker.formal_artifacts import GateFailure
    from ephy_worker.formal_runtime import exclusive_lock
    from ephy_worker.owner_repair_pipeline import main
    lock = tmp_path / "resource.lock"
    job = tmp_path / "job.json"
    job.write_text(json.dumps({"runtime": {"python": sys.executable}}), encoding="utf-8")
    ran = []
    class Runner:
        def __init__(self):
            self.runtime = {"resource_lock": str(lock)}
        def run(self):
            with pytest.raises(GateFailure, match="owns the resource lock"), exclusive_lock(lock):
                pytest.fail("the formal model stage ran without its singleton lock")
            ran.append(True)
    monkeypatch.setitem(sys.modules, "ephy_worker.strata_runtime", SimpleNamespace(make_runner=lambda *_args, **_kwargs: Runner()))
    monkeypatch.setattr(sys, "argv", ["owner_repair_pipeline", "--run-formal", str(job)])
    main()
    assert ran == [True]


def test_runtime_controls_use_runtime_python_and_candidate_root(tmp_path, monkeypatch):
    source = tmp_path / "runtime"
    (source / "tests").mkdir(parents=True)
    (source / "packages").mkdir()
    (source / "tests/test_x.py").write_text("def test_x():\n    assert True\n")
    (source / "packages/example.py").write_text("answer = 0\n")
    monkeypatch.setattr("ephy_worker.owner_repair_discovery.snapshot", lambda _: {
        "tests/test_x.py": {"sha256": "irrelevant-fixture"}, "packages/example.py": {"sha256": "irrelevant-fixture"}})
    calls = []
    def run_isolated(_descriptor, argv, **kwargs):
        calls.append((argv, kwargs))
        exit_code = 0 if kwargs["candidate"].name == "known_good" else 1
        _review_v13_fixture_completion(argv, exit_code)
        return {"exit_code": exit_code}
    monkeypatch.setitem(sys.modules, "ephy_worker.verifier_sandbox", SimpleNamespace(run_isolated=run_isolated))
    directory = tmp_path / "execution" / "request"
    directory.mkdir(parents=True)
    checker = directory / "checker.py"
    checker.write_text("# fixture checker")
    chosen_python = str(tmp_path / "runtime-python.exe")
    discovery = LocalDiscovery(SimpleNamespace(state_dir=tmp_path / "state", python="wrong-controller-python"))
    prepared = {"known_good": {"tests/test_x.py": "def test_x():\n    assert True\n", "packages/example.py": "answer = 1\n"},
        "wrong_answer": {"packages/example.py": "answer = -1\n"}}
    results = discovery.measure_controls(SimpleNamespace(source=source, github_repository="kirimine170/ephy-runtime"),
        directory, prepared, checker, {"runtime": {"python": chosen_python}, "contract": {"verifier_sandbox": {}}})
    assert len(results) == len(calls) == 5
    for argv, invocation in calls:
        assert argv[:2] == [chosen_python, "-I"]
        assert invocation["environment"]["PYTHONPATH"] == str(invocation["candidate"])


@pytest.mark.asyncio
async def test_drain_watch_acknowledges_only_current_nonce(tmp_path):
    bridge, _, _ = bridge_fixture(tmp_path)
    marker = bridge.settings.state_dir / "repair-drain"
    marker.write_text("old-nonce")
    bridge.activity()
    marker.write_text("new-deployment-nonce")
    activity = bridge.settings.state_dir / "repair-active.json"
    assert json.loads(activity.read_text())["drain_token"] == "old-nonce"
    watcher = asyncio.create_task(bridge.watch_drain(interval=0.01))
    try:
        await asyncio.sleep(0.02)
        state = json.loads(activity.read_text())
        assert state["drain_token"] == "new-deployment-nonce" and state["active"] == 0
    finally:
        watcher.cancel()
        await asyncio.gather(watcher, return_exceptions=True)


@pytest.mark.asyncio
async def test_request_waiting_for_ledger_is_not_admitted_after_drain_marker(tmp_path, monkeypatch):
    bridge, gateway, event = bridge_fixture(tmp_path)
    event = replace(event, user_id=533693210879459338, content="<@10> 公開の質問")
    def previous(_):
        (bridge.settings.state_dir / "repair-drain").write_text("drain-after-await")
    monkeypatch.setattr(bridge.ledger, "previous", previous)
    sent = []
    async def send(text):
        sent.append(text)
    await bridge.handle(event, send)
    assert gateway.classifications == [] and gateway.answers == [] and sent == []
    assert bridge.repairs.next() is None
    state = json.loads((bridge.settings.state_dir / "repair-active.json").read_text())
    assert state["active"] == 0 and state["drain_token"] == "drain-after-await"
    assert bridge.ledger.inbox_pending()


@pytest.mark.asyncio
async def test_drain_waits_for_existing_generation_without_cancelling_it(tmp_path):
    bridge, gateway, event = bridge_fixture(tmp_path)
    started, finish = asyncio.Event(), asyncio.Event()
    async def answer(*_):
        started.set()
        await finish.wait()
        return "既存の処理は最後まで回答しました．"
    gateway.answer = answer
    sent = []
    async def send(text):
        sent.append(text)
        return 1234
    handler = asyncio.create_task(bridge.handle(replace(event, user_id=533693210879459338,
        content="<@10> 公開情報を調べて"), send))
    await started.wait()
    (bridge.settings.state_dir / "repair-drain").write_text("wait-existing")
    watcher = asyncio.create_task(bridge.watch_drain(interval=0.01))
    try:
        await asyncio.sleep(0.02)
        state = json.loads((bridge.settings.state_dir / "repair-active.json").read_text())
        assert state["drain_token"] == "wait-existing" and state["active"] == 1 and not handler.done()
        finish.set()
        await handler
        state = json.loads((bridge.settings.state_dir / "repair-active.json").read_text())
        assert state["active"] == 0 and sent[-1] == "既存の処理は最後まで回答しました．"
    finally:
        finish.set()
        await asyncio.gather(handler, return_exceptions=True)
        watcher.cancel()
        await asyncio.gather(watcher, return_exceptions=True)


@pytest.mark.asyncio
async def test_drain_counts_outbox_send_and_pauses_unclaimed_chunks(tmp_path):
    bridge, _, _ = bridge_fixture(tmp_path)
    identifier = bridge.repairs.enqueue(**directive())
    bridge.repairs.finish(identifier, "completed", "完了結果です．" * 500)
    started, finish = asyncio.Event(), asyncio.Event()
    sent = []
    async def send(row):
        sent.append(row["ordinal"])
        started.set()
        await finish.wait()
        return 999 + row["ordinal"]
    delivery = asyncio.create_task(bridge.deliver_outbox(send))
    await started.wait()
    marker = bridge.settings.state_dir / "repair-drain"
    marker.write_text("outbox-nonce")
    watcher = asyncio.create_task(bridge.watch_drain(interval=0.01))
    try:
        await asyncio.sleep(0.02)
        state = json.loads((bridge.settings.state_dir / "repair-active.json").read_text())
        assert state["active"] == 1 and state["drain_token"] == "outbox-nonce"
        finish.set()
        await delivery
        assert sent == [0]
        pending = bridge.repairs.pending(guild_id=11, channel_ids=(20,))
        assert pending and all(row["ordinal"] > 0 for row in pending)
        await bridge.deliver_outbox(send)
        assert sent == [0]
        state = json.loads((bridge.settings.state_dir / "repair-active.json").read_text())
        assert state["active"] == 0 and state["drain_token"] == "outbox-nonce"
        marker.unlink()
        await bridge.deliver_outbox(send)
        assert len(sent) > 1
    finally:
        finish.set()
        await asyncio.gather(delivery, return_exceptions=True)
        watcher.cancel()
        await asyncio.gather(watcher, return_exceptions=True)


def test_isolated_checker_rejects_stdlib_shadow_before_candidate_imports(tmp_path):
    import inspect

    from ephy_worker.formal_artifacts import file_hash, write_json
    from ephy_worker.formal_runtime import command_environment
    from ephy_worker.owner_repair_discovery import _CHECKER
    candidate, checker_root = tmp_path / "candidate", tmp_path / "checker"
    (candidate / "src").mkdir(parents=True)
    checker_root.mkdir()
    feature = candidate / "src/feature.py"
    feature.write_text("value = 1\n")
    (candidate / "src/ast.py").write_text("raise SystemExit(0)\n")
    write_json(checker_root / "checker-guard.json", {"allowed": ["src/feature.py"],
        "snapshot": {"src/feature.py": {"sha256": file_hash(feature)}}, "tests": {}, "authority": {}})
    checker = checker_root / "checker.py"
    checker.write_text("import ast\n" + inspect.getsource(authority_fingerprint) + "\n" + _CHECKER, encoding="utf-8")
    (checker_root / "acceptance.py").write_text("raise SystemExit(0)\n")
    environment = command_environment(candidate, tmp_path)
    vulnerable = subprocess.run([sys.executable, str(checker), str(candidate)], env=environment,
        cwd=candidate, capture_output=True, timeout=30, check=False)
    isolated = subprocess.run([sys.executable, "-I", str(checker), str(candidate)], env=environment,
        cwd=candidate, capture_output=True, timeout=30, check=False)
    assert vulnerable.returncode == 0
    assert isolated.returncode != 0 and b"scope_escape" in isolated.stderr


def test_compact_control_append_preserves_large_original_tests_without_repeating_them():
    original = "# existing regression fixture\n" * 2500 + "def test_existing():\n    assert 1 == 1\n"
    small = {"tests/test_large.py": [{"append": "\ndef test_new_regression():\n    assert 2 == 2\n"}]}
    expanded = expand_control_edits(small, {"tests/test_large.py": original})
    assert len(json.dumps(small)) < 200 and len(expanded["tests/test_large.py"]) > 70000
    assert expanded["tests/test_large.py"].startswith(original)
    assert "def test_new_regression" in expanded["tests/test_large.py"]


@pytest.mark.parametrize("edits", [{"x.py": [{"old": "repeat", "new": "fixed"}]},
    {"x.py": [{"old": "missing", "new": "fixed"}]}, {"x.py": [{"old": "", "new": "fixed"}]},
    {"x.py": [{"append": "text", "owner": True}]}, {"other.py": [{"append": "escape"}]},
    {"x.py": [{"command": "arbitrary shell"}]}])
def test_ambiguous_or_out_of_scope_control_edits_are_rejected(edits):
    from ephy_worker.formal_artifacts import GateFailure
    with pytest.raises(GateFailure):
        expand_control_edits(edits, {"x.py": "repeat\nrepeat\n"})


@pytest.mark.parametrize("too_large", [False, True])
def test_discovery_uses_actual_template_tokens_and_rejects_context_before_inference(tmp_path, monkeypatch, too_large):
    from ephy_worker.formal_artifacts import GateFailure
    identity = tmp_path / "identity.json"
    identity.write_text(json.dumps({"context": 4100 if too_large else 5000, "model_id": "fixture-model"}), encoding="utf-8")
    monkeypatch.setitem(sys.modules, "ephy_worker.existing_llama_runtime", SimpleNamespace(verify_identity=lambda _: None))
    calls = []
    def handler(request):
        data = json.loads(request.content)
        calls.append((request.url.path, data))
        if request.url.path == "/apply-template":
            return httpx.Response(200, json={"prompt": "actual formatted fixture prompt"})
        if request.url.path == "/tokenize":
            assert data == {"content": "actual formatted fixture prompt", "add_special": False}
            return httpx.Response(200, json={"tokens": list(range(10))})
        assert request.url.path == "/v1/chat/completions"
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": '{"fixture":true}'}}]})
    actual_client = httpx.Client
    monkeypatch.setattr("ephy_worker.owner_repair_discovery.httpx.Client", lambda **kwargs:
        actual_client(transport=httpx.MockTransport(handler), **kwargs))
    discovery = LocalDiscovery(SimpleNamespace())
    runtime = {"models_ini": str(identity), "base_url": "http://127.0.0.1:18080"}
    if too_large:
        with pytest.raises(GateFailure, match="context_budget_exceeded"):
            discovery.completion("source fixture", runtime)
        assert [path for path, _ in calls] == ["/apply-template", "/tokenize"]
    else:
        assert discovery.completion("source fixture", runtime) == {"fixture": True}
        assert calls[-1][1]["max_tokens"] == 5000 - 10 - 64
        assert calls[-1][1]["messages"] == calls[0][1]["messages"]


def test_all_discovery_control_argv_explicitly_select_utf8_with_real_japanese_output(tmp_path, monkeypatch):
    # Only the isolation transport is replaced; each of the five unit-control
    # child interpreters actually runs -I -X utf8 and emits Japanese text.
    source = tmp_path / "source"
    (source / "tests").mkdir(parents=True)
    relative = "tests/test_utf8_fixture.py"
    original = "def test_original():\n    assert True\n"
    (source / relative).write_text(original, encoding="utf-8")
    monkeypatch.setattr("ephy_worker.owner_repair_discovery.snapshot", lambda _: {relative: {"sha256": "fixture"}})
    directory = tmp_path / "execution" / "utf8-controls"
    directory.mkdir(parents=True)
    checker = directory / "checker.py"
    checker.write_text("import sys\nfrom pathlib import Path\nassert sys.flags.utf8_mode == 1\n"
        "print('日本語の検証結果')\nraise SystemExit(0 if Path(sys.argv[1]).name == 'known_good' else 1)\n",
        encoding="utf-8")
    calls = []
    def run_isolated(_descriptor, argv, **kwargs):
        requested = json.loads(Path(argv[argv.index("--request") + 1]).read_text())["argv"]
        calls.append((requested, kwargs))
        result = subprocess.run(requested, cwd=kwargs["cwd"], env=kwargs["environment"],
            capture_output=True, check=False, timeout=30)
        assert result.stdout.decode("utf-8").strip() == "日本語の検証結果"
        assert not result.stderr
        _review_v13_fixture_completion(argv, result.returncode)
        return {"exit_code": result.returncode, "stdout": result.stdout.decode("utf-8"), "stderr": ""}
    monkeypatch.setitem(sys.modules, "ephy_worker.verifier_sandbox", SimpleNamespace(run_isolated=run_isolated))
    discovery = LocalDiscovery(SimpleNamespace(state_dir=tmp_path / "state"))
    prepared = {"known_good": {relative: original + "\ndef test_added():\n    assert True\n"},
        "wrong_answer": {relative: original}}
    results = discovery.measure_controls(SimpleNamespace(source=source, github_repository="kirimine170/ephy-worker"),
        directory, prepared, checker, {"runtime": {"python": sys.executable}, "contract": {"verifier_sandbox": {}}})
    names = ["known_good", "unchanged", "wrong_answer", "test_weakening", "scope_escape"]
    assert [result["name"] for result in results] == names
    assert [invocation["candidate"].name for _, invocation in calls] == names
    for argv, invocation in calls:
        assert argv == [sys.executable, "-I", "-X", "utf8", str(checker), str(invocation["candidate"])]


def test_isolated_explicit_utf8_child_reports_actual_mode_and_japanese_bytes():
    result = subprocess.run([sys.executable, "-I", "-X", "utf8", "-c",
        "import sys; assert sys.flags.utf8_mode == 1; print('独立した日本語出力')"],
        capture_output=True, check=False, timeout=30)
    assert result.returncode == 0 and not result.stderr
    assert result.stdout.decode("utf-8").strip() == "独立した日本語出力"


@pytest.mark.parametrize("point", ["receipt", "classification", "before_enqueue", "after_enqueue",
    "before_claim", "after_claim", "acceptance"])
@pytest.mark.asyncio
async def test_review_revision_owner_intake_crash_replays_without_losing_or_duplicating_repair(tmp_path, monkeypatch, point):
    bridge, gateway, event = bridge_fixture(tmp_path)
    enqueue, claim, classify = bridge.repairs.enqueue, bridge.ledger.claim, gateway.classify_repair
    queued_points = {"after_enqueue", "before_claim", "after_claim", "acceptance"}
    claimed_points = {"after_claim", "acceptance"}

    def interrupted_enqueue(**kwargs):
        if point == "before_enqueue":
            raise asyncio.CancelledError
        identifier = enqueue(**kwargs)
        if point == "after_enqueue":
            raise asyncio.CancelledError
        return identifier

    def interrupted_claim(message):
        assert bridge.repairs.next() is not None, "claim preceded durable repair enqueue"
        if point == "before_claim":
            raise asyncio.CancelledError
        result = claim(message)
        if point == "after_claim":
            raise asyncio.CancelledError
        return result

    async def interrupted_classify(text, previous):
        if point == "classification":
            raise asyncio.CancelledError
        return await classify(text, previous)

    async def interrupted_send(text):
        if (point == "receipt" and text == RECEIPT) or (point == "acceptance" and "修正依頼として" in text):
            raise asyncio.CancelledError
        return 1000

    with monkeypatch.context() as patch:
        patch.setattr(bridge.repairs, "enqueue", interrupted_enqueue)
        patch.setattr(bridge.ledger, "claim", interrupted_claim)
        patch.setattr(gateway, "classify_repair", interrupted_classify)
        with pytest.raises(asyncio.CancelledError):
            await bridge.handle(event, interrupted_send)
    before = bridge.repairs.next()
    assert (before is not None) == (point in queued_points)
    assert bridge.ledger.seen(event.message_id) == (point in claimed_points)
    assert bridge.inflight == 0

    restarted, restarted_gateway, replay = bridge_fixture(tmp_path)
    sent = []
    async def send(text):
        sent.append(text)
        return 1001
    await restarted.handle(replay, send)
    queued = restarted.repairs.next()
    assert queued is not None and queued["stage"] == "queued"
    if before is not None:
        assert queued["id"] == before["id"]
    with restarted.repairs.db() as db:
        assert db.execute("SELECT count(*) FROM jobs").fetchone()[0] == 1
    assert restarted.ledger.seen(event.message_id)
    assert len(restarted_gateway.classifications) == (0 if point in claimed_points else 1)
    assert restarted_gateway.answers == []
    sent_before = list(sent)
    await restarted.handle(replay, send)
    assert sent == sent_before


@pytest.mark.asyncio
async def test_review_revision_concurrent_owner_delivery_enqueues_one_durable_job_before_claim(tmp_path):
    first, _, event = bridge_fixture(tmp_path)
    second, _, _ = bridge_fixture(tmp_path)
    ready, finish = asyncio.Event(), asyncio.Event()
    classifiers = []
    claims = []
    sent = []

    async def classify(text, previous):
        classifiers.append((text, previous))
        ready.set()
        await finish.wait()
        return True

    def guarded_claim(bridge):
        original = bridge.ledger.claim
        def claim(message):
            assert bridge.repairs.next() is not None
            result = original(message)
            claims.append(result)
            return result
        return claim

    for bridge in (first, second):
        bridge.gateway.classify_repair = classify
        bridge.ledger.claim = guarded_claim(bridge)
    async def send(text):
        sent.append(text)
        return len(sent) + 1000
    handler = asyncio.create_task(first.handle(event, send))
    await asyncio.wait_for(ready.wait(), 10)
    await second.handle(event, send)
    finish.set()
    await asyncio.wait_for(handler, 10)
    with first.repairs.db() as db:
        assert db.execute("SELECT count(*) FROM jobs").fetchone()[0] == 1
    assert len(classifiers) == 1 and claims == [True]
    assert sum("修正依頼として" in text for text in sent) == 1
    assert first.ledger.seen(event.message_id) and first.inflight == second.inflight == 0


@pytest.mark.asyncio
async def test_review_revision_owner_conversation_and_classifier_failure_remain_replayable_before_claim(tmp_path):
    bridge, gateway, event = bridge_fixture(tmp_path, repair=False)
    sent = []
    async def send(text):
        sent.append(text)
        return 1000
    await bridge.handle(event, send)
    assert not bridge.ledger.seen(event.message_id) and bridge.repairs.next() is None
    assert gateway.answers == [] and "公開の質問" in sent[-1]
    event = replace(event, message_id=event.message_id + 1)
    public = replace(event, content="<@10> 先ほどの回答の数字はどうですか")
    await bridge.handle(public, send)
    assert bridge.ledger.seen(event.message_id) and bridge.repairs.next() is None
    assert len(gateway.answers) == 1
    await bridge.handle(public, send)
    assert len(gateway.answers) == 1 and len(gateway.classifications) == 2

    broken, broken_gateway, private = bridge_fixture(tmp_path / "classification-failure", failure=True)
    await broken.handle(private, send)
    assert not broken.ledger.seen(private.message_id) and broken.repairs.next() is None
    assert broken_gateway.answers == [] and "判定できません" in sent[-1]
    broken_gateway.failure = False
    await broken.handle(private, send)
    assert broken.ledger.seen(private.message_id) and broken.repairs.next() is not None
    assert len(broken_gateway.classifications) == 2 and broken_gateway.answers == []


@pytest.mark.parametrize("author", [533693210879459338, 330903316575944704])
@pytest.mark.asyncio
async def test_review_revision_nonowner_private_directive_cannot_enter_repair_intake(tmp_path, author):
    bridge, gateway, event = bridge_fixture(tmp_path)
    sent = []
    async def send(text):
        sent.append(text)
    await bridge.handle(replace(event, user_id=author), send)
    assert gateway.classifications == [] and gateway.answers == []
    assert bridge.repairs.next() is None and not bridge.ledger.seen(event.message_id)
    assert "公開の質問" in sent[-1]


def _review_revision_collection_cases():
    return [
        ("@pytest.fixture(autouse=True)\ndef helper_added():\n    pass\n", "decorator"),
        ("@fixture_alias\ndef helper_added():\n    pass\n", "decorator"),
        ("@trip()\ndef helper_added():\n    pass\n", "decorator"),
        ("def helper_added(value=trip()):\n    pass\n", "default"),
        ("def helper_added(*, value=trip()):\n    pass\n", "default"),
        ("def helper_added(value=1):\n    pass\n", "default"),
        ("def helper_added(value: trip(), /):\n    pass\n", "annotation"),
        ("def helper_added(value: trip()):\n    pass\n", "annotation"),
        ("def helper_added(*, value: trip()):\n    pass\n", "annotation"),
        ("def helper_added(*values: trip()):\n    pass\n", "annotation"),
        ("def helper_added(**values: trip()):\n    pass\n", "annotation"),
        ("def helper_added() -> trip():\n    pass\n", "annotation"),
        ("def helper_added(value: 'trip()'):\n    pass\n", "annotation"),
        ("def helper_added[T: trip()]():\n    pass\n", "type_parameter"),
        ("def helper_added[*Ts]():\n    pass\n", "type_parameter"),
        ("def helper_added[**P]():\n    pass\n", "type_parameter"),
        ("@trip()\nasync def helper_added():\n    pass\n", "decorator"),
        ("async def helper_added(value=trip()):\n    pass\n", "default"),
        ("async def helper_added() -> trip():\n    pass\n", "annotation"),
        ("async def helper_added[T: trip()]():\n    pass\n", "type_parameter"),
    ]


def _review_revision_run_generated_checker(tmp_path, original, candidate_text):
    import hashlib
    import inspect

    from ephy_worker.owner_repair_discovery import _CHECKER

    candidate, directory = tmp_path / "candidate", tmp_path / "checker"
    test = candidate / "tests/test_original.py"
    test.parent.mkdir(parents=True)
    directory.mkdir()
    test.write_text(candidate_text, encoding="utf-8")
    (directory / "checker-guard.json").write_text(json.dumps({"allowed": ["tests/test_original.py"],
        "snapshot": {"tests/test_original.py": {"sha256": hashlib.sha256(original.encode()).hexdigest()}},
        "authority": {}, "tests": {"tests/test_original.py": original}}), encoding="utf-8")
    checker = directory / "checker.py"
    checker.write_text("import ast\n" + inspect.getsource(authority_fingerprint) + "\n"
        + inspect.getsource(preserve_original_tests) + "\n" + _CHECKER, encoding="utf-8")
    marker = tmp_path / "acceptance-was-reached"
    (directory / "acceptance.py").write_text("from pathlib import Path\nPath(" + repr(str(marker))
        + ").write_text('reached', encoding='utf-8')\n", encoding="utf-8")
    result = subprocess.run([sys.executable, "-I", "-X", "utf8", "-B", str(checker), str(candidate)],
        capture_output=True, text=True, encoding="utf-8", check=False, timeout=30)
    return result, marker


@pytest.mark.parametrize("appended, error", _review_revision_collection_cases())
def test_review_revision_direct_and_generated_checker_reject_collection_execution(tmp_path, appended, error):
    original = "import pytest\n\n@pytest.fixture\ndef stable():\n    return 1\n\ndef test_original(stable):\n    assert stable == 1\n"
    candidate = original + "\n" + appended
    with pytest.raises(ValueError, match="new_test_collection_" + error):
        preserve_original_tests(original, candidate)
    result, marker = _review_revision_run_generated_checker(tmp_path, original, candidate)
    assert result.returncode != 0 and "new_test_collection_" + error in result.stderr
    assert not marker.exists()


def test_review_revision_direct_and_generated_checker_allow_inert_new_function_signatures(tmp_path):
    original = "from __future__ import annotations\n\nimport pytest\n\n@pytest.fixture\ndef stable():\n    return 1\n\ndef test_original(stable):\n    assert stable == 1\n"
    appended = ("\ndef helper_added(value, /, *items, flag, **kw):\n    return value + 1\n"
        "\nasync def helper_async(value):\n    return value\n"
        "\ndef test_new_regression(stable):\n    assert helper_added(stable, flag=True) == 2\n")
    preserve_original_tests(original, original + appended)
    result, marker = _review_revision_run_generated_checker(tmp_path, original, original + appended)
    assert result.returncode == 0, result.stderr
    assert marker.read_text(encoding="utf-8") == "reached"


@pytest.mark.parametrize("body", [
    "import os\n    os._exit(0)",
    "import os as hidden\n    hidden._exit(0)",
    "from os import _exit as hidden\n    hidden(0)",
    "if False:\n        import os\n        os._exit(0)\n    assert True",
    "assert os._exit(0) is None",
    "assert sys.exit(0) is None",
    "raise SystemExit(0)",
    "assert exit(0) is None",
    "assert eval('1') == 1",
    "assert getattr(str, 'upper')('x') == 'X'",
    "assert __import__('os') is None",
    "fn = os._exit\n    assert fn(0) is None",
    "fn = len\n    assert fn([]) == 0",
    "assert (lambda: 1)() == 1",
    "assert [len][0]([]) == 0",
    "assert ''.__class__ is str",
    "global changed\n    changed = 1\n    assert True",
    "values = []\n    values.append(1)\n    assert values == [1]",
    "values = [0]\n    values[0] = 1\n    assert values == [1]",
    "try:\n        assert False\n    except Exception:\n        return",
    "with open('file', 'w') as stream:\n        assert True",
    "while True:\n        assert True",
    "for value in [1]:\n        assert value == 1",
    "def nested():\n        return 1\n    assert nested() == 1",
    "class Nested:\n        pass\n    assert True",
    "assert 2 ** 1000000 > 0",
    "assert len('x' * 1000000000) > 0",
    "assert '%1000000000s' % 0",
    "assert [value for value in range(1000000)]",
    "assert [[x for x in range(100)] for y in range(100)]",
])
def test_review_v11_direct_and_emitted_body_guard_rejects_capabilities_before_acceptance(tmp_path, body):
    original = "import os\nimport sys\n\ndef test_original():\n    assert 1 == 1\n"
    candidate = original + "\ndef test_unsafe():\n    " + body + "\n"
    with pytest.raises(ValueError, match="new_test_body_"):
        preserve_original_tests(original, candidate)
    result, marker = _review_revision_run_generated_checker(tmp_path, original, candidate)
    assert result.returncode != 0 and "new_test_body_" in result.stderr
    assert not marker.exists()


@pytest.mark.parametrize("appended", [
    "def helper_callback(fn):\n    return fn()\n",
    "def helper_first(value):\n    return helper_second(value)\n\ndef helper_second(value):\n    return helper_first(value)\n",
    "def helper_async_call(value):\n    return helper_async(value)\n\nasync def helper_async(value):\n    return value\n",
    "def test_capability_fixture(monkeypatch):\n    assert True\n",
    "def test_capability_fixture(request):\n    assert True\n",
    "def pytest_sessionfinish():\n    return 0\n",
    "def helper_mutate(values):\n    values.clear()\n    return values\n",
])
def test_review_v11_helpers_fixtures_and_hooks_cannot_confer_capabilities(tmp_path, appended):
    original = "def test_original():\n    assert 1 == 1\n"
    with pytest.raises(ValueError, match="new_test_body_"):
        preserve_original_tests(original, original + "\n" + appended)
    result, marker = _review_revision_run_generated_checker(tmp_path, original, original + "\n" + appended)
    assert result.returncode != 0 and not marker.exists()


def test_review_v11_pure_bounded_computation_and_literal_fixture_are_usable(tmp_path):
    original = "import pytest\n\n@pytest.fixture\ndef stable():\n    return 1\n\ndef test_original(stable):\n    assert stable == 1\n"
    appended = ("\ndef helper_increment(value):\n    return value + 1\n"
        "\nasync def helper_async(value):\n    return value\n"
        "\ndef test_bounded(stable):\n    values = [value + 1 for value in range(3)]\n"
        "    label = ' value '\n    assert label.strip() == 'value'\n"
        "    assert values == [1, 2, 3]\n    assert helper_increment(stable) == 2\n")
    preserve_original_tests(original, original + appended)
    result, marker = _review_revision_run_generated_checker(tmp_path, original, original + appended)
    assert result.returncode == 0, result.stderr
    assert marker.read_text(encoding="utf-8") == "reached"


def test_review_v11_registered_subject_calls_are_exact_and_shadowed_imports_reject():
    from ephy_worker.owner_repair_discovery import registered_test_subjects
    source = "def answer(value):\n    return value + 1\n"
    original = "from ephy_worker.feature import answer as feature_answer\n\ndef test_original():\n    assert True\n"
    subjects = registered_test_subjects(original, {"src/ephy_worker/feature.py": source})
    assert subjects == ("feature_answer",)
    candidate = original + "\ndef test_regression():\n    assert feature_answer(1) == 2\n"
    preserve_original_tests(original, candidate, subjects)
    with pytest.raises(ValueError, match="capability"):
        preserve_original_tests(original, candidate)
    assert registered_test_subjects(original + "feature_answer = None\n", {"src/ephy_worker/feature.py": source}) == ()
    with pytest.raises(ValueError):
        preserve_original_tests(original, original + "\ndef test_alias():\n    fn = feature_answer\n    assert fn(1) == 2\n", subjects)


def test_review_v11_existing_global_alias_cannot_shadow_a_pure_builtin():
    original = "from os import _exit as len\n\ndef test_original():\n    assert True\n"
    with pytest.raises(ValueError, match="capability"):
        preserve_original_tests(original, original + "\ndef test_unsafe():\n    assert len(0) is None\n")


def test_review_v11_actual_prepare_emits_identical_body_guard_and_explicit_feature_bindings(tmp_path, monkeypatch):
    import inspect

    from ephy_worker.formal_artifacts import GateFailure, file_hash, write_json

    source = tmp_path / "source"
    source_file = source / "src/ephy_worker/feature.py"
    test_file = source / "tests/test_feature.py"
    source_file.parent.mkdir(parents=True)
    test_file.parent.mkdir(parents=True)
    source_file.write_text("def answer(value):\n    return value\n", encoding="utf-8")
    (source_file.parent / "__init__.py").write_text("", encoding="utf-8")
    original = "from ephy_worker.feature import answer\n\ndef test_original():\n    assert answer(0) >= 0\n"
    test_file.write_text(original, encoding="utf-8")
    home = tmp_path / "synthetic-git-home"
    home.mkdir()
    for name in ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA"):
        monkeypatch.setenv(name, str(home))
    subprocess.run(["git", "-C", str(source), "init"], check=True, capture_output=True, timeout=30)
    subprocess.run(["git", "-C", str(source), "add", "."], check=True, capture_output=True, timeout=30)
    names = ["src/ephy_worker/feature.py", "tests/test_feature.py"]
    template = tmp_path / "template.json"
    write_json(template, {"runtime": {}, "contract": {}})
    profile = SimpleNamespace(source=source, formal_template=template, formal_template_sha256=file_hash(template),
        allowed_prefixes=("src/", "tests/"), origin="https://github.com/kirimine170/ephy-worker.git")
    discovery = LocalDiscovery(SimpleNamespace(repositories={"ephy-worker": profile}))
    responses = iter([{"repository": "ephy-worker", "allowed_files": names, "semantic_scope": "Correct feature answer"},
        {"checker_python": "from ephy_worker.feature import answer\nassert answer(1) == 2\n",
            "known_good": {names[0]: [{"old": "return value", "new": "return value + 1"}],
                names[1]: [{"append": "\ndef test_regression():\n    assert answer(1) == 2\n"}]},
            "wrong_answer": {names[0]: [{"old": "return value", "new": "return value - 1"}],
                names[1]: [{"append": "\ndef test_regression():\n    assert answer(1) == 2\n"}]}}])
    prompts = []
    def completion(prompt, _runtime):
        prompts.append(prompt)
        return next(responses)
    monkeypatch.setattr(discovery, "completion", completion)
    def git(_root, *args):
        if args[0] == "ls-files":
            return ("\n".join(names) + "\n").encode()
        if args[0] == "remote":
            return profile.origin.encode()
        return b"" if args[0] == "status" else b"d" * 40
    monkeypatch.setattr(discovery, "_git", git)
    observed = []
    def controls(_profile, directory, prepared, checker, _template):
        tree = ast.parse(checker.read_text(encoding="utf-8"))
        emitted = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "preserve_original_tests")
        assert ast.dump(emitted) == ast.dump(ast.parse(inspect.getsource(preserve_original_tests)).body[0])
        guard = json.loads((directory / "checker-guard.json").read_text(encoding="utf-8"))
        assert guard["subjects"] == {names[1]: ["answer"]}
        for label in ("known_good", "wrong_answer", "unsafe"):
            root = tmp_path / label
            for name in ["src/ephy_worker/__init__.py", *names]:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                replacement = prepared["wrong_answer" if label == "wrong_answer" else "known_good"].get(name)
                path.write_text((source / name).read_text(encoding="utf-8") if replacement is None else replacement, encoding="utf-8")
            if label == "unsafe":
                (root / names[1]).write_text(original + "\ndef test_abort():\n    import os\n    os._exit(0)\n", encoding="utf-8")
            result = subprocess.run([sys.executable, "-I", "-X", "utf8", "-B", str(checker), str(root)],
                capture_output=True, text=True, encoding="utf-8", check=False, timeout=30)
            observed.append((label, result.returncode))
            assert (result.returncode == 0) == (label == "known_good"), result.stderr
            if label == "unsafe":
                assert "new_test_body_unsupported_statement" in result.stderr
        raise GateFailure("synthetic_prepare_observation_complete")
    monkeypatch.setattr(discovery, "measure_controls", controls)
    with pytest.raises(GateFailure, match="synthetic_prepare_observation_complete"):
        discovery.prepare({"text": "synthetic repair", "previous": None}, tmp_path / "discovery")
    assert len(observed) == 3
    assert "No imports anywhere" in prompts[1]
    assert json.loads(prompts[1])["approved_subject_calls_by_test_file"] == {names[1]: ["answer"]}


def _review_v13_fixture_completion(argv, exit_code):
    """The existing fake isolation transport supplies the new child protocol．"""
    from ephy_worker.check_completion import _bytes, _hash
    request_path = Path(argv[argv.index("--request") + 1])
    request = json.loads(request_path.read_bytes())
    record = {"schema": "ephy.check-completion-record.v1", "nonce": request["nonce"],
        "label": request["label"], "request_sha256": _hash(request_path.read_bytes()),
        "argv_sha256": _hash(_bytes(request["argv"])), "cwd": request["cwd"],
        "exit_code": exit_code, "reported_pid": os.getpid(), "outcome": "returned"}
    Path(request["record"]).write_bytes(_bytes(record))


@pytest.mark.parametrize("phase", ["before_receipt", "during_classification"])
@pytest.mark.asyncio
async def test_review_v13_real_process_crash_recovers_pending_without_event_replay(tmp_path, phase):
    from ephy_worker.formal_runtime import command_environment
    state = tmp_path / "repair"
    payload = directive()
    code = ("import os,psutil\nfrom pathlib import Path\nfrom ephy_worker.owner_repair import RepairStore\n"
        + "store=RepairStore(Path(" + repr(str(state)) + "))\n"
        + "identifier=store.capture_intake(**" + repr(payload) + ")\n"
        + "consumer=str(os.getpid())+':'+str(psutil.Process().create_time())\n"
        + "store.claim_intake(identifier,consumer)\n"
        + ("store.update_intake(identifier,consumer,receipt=True)\n" if phase == "during_classification" else "")
        + "os._exit(0)\n")
    environment = command_environment(tmp_path)
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    child = await asyncio.to_thread(subprocess.run, [sys.executable, "-X", "utf8", "-B", "-c", code],
        env=environment, cwd=tmp_path, capture_output=True, check=False, timeout=30)
    assert child.returncode == 0, child.stderr
    bridge, gateway, _ = bridge_fixture(tmp_path)
    sent = []
    async def send(row, text):
        assert row["author"] == str(OWNER_USER_ID)
        sent.append(text)
        return len(sent) + 9000
    await bridge.recover_intakes(send)
    await bridge.recover_intakes(send)
    assert gateway.classifications == [(payload["text"], None)] and gateway.answers == []
    with bridge.repairs.db() as db:
        assert db.execute("SELECT count(*) FROM jobs").fetchone()[0] == 1
        assert db.execute("SELECT done FROM intakes").fetchone()[0] == 1
    assert sum("修正依頼として" in text for text in sent) == 1
    assert bridge.ledger.seen(payload["message_id"])


@pytest.mark.asyncio
async def test_review_v13_capture_is_durable_at_first_receipt_and_concurrent_recovery_dedups(tmp_path):
    bridge, gateway, event = bridge_fixture(tmp_path)
    started, finish = asyncio.Event(), asyncio.Event()
    async def classify(text, previous):
        gateway.classifications.append((text, previous))
        started.set()
        await finish.wait()
        return True
    gateway.classify_repair = classify
    sent = []
    async def send(text):
        with bridge.repairs.db() as db:
            assert db.execute("SELECT count(*) FROM intakes").fetchone()[0] == 1
        sent.append(text)
        return 9000
    first = asyncio.create_task(bridge.handle(event, send))
    await asyncio.wait_for(started.wait(), 5)
    await bridge.handle(event, send)
    async def recovered(_row, text):
        return await send(text)
    await bridge.recover_intakes(recovered)
    finish.set()
    await first
    assert len(gateway.classifications) == 1 and sum("修正依頼として" in text for text in sent) == 1
    assert bridge.repairs.next() is not None


@pytest.mark.parametrize("text", ["修正して，マージしないで", "修正して，本番には反映しないで", "修正して，反映は不要",
    "修正して，再起動はしないで", "修正して，コミットせずに", "fix it; no-merge", "fix it; do not deploy",
    "fix it; don't push", "fix it; no commit", "fix it; no restart", "proposal-only repair"])
@pytest.mark.asyncio
async def test_review_v13_request_limits_stop_automatic_full_cycle_before_enqueue(tmp_path, text):
    bridge, gateway, event = bridge_fixture(tmp_path)
    sent = []
    async def send(value):
        sent.append(value)
    await bridge.handle(replace(event, content="<@10> " + text), send)
    assert len(gateway.classifications) == 1 and bridge.repairs.next() is None and gateway.answers == []
    assert "操作の制限" in sent[-1] and "開始していません" in sent[-1]


@pytest.mark.parametrize("name", ["tests/conftest.py", "tests/test_new.py", "src/ephy_worker/pytest_hidden.py",
    "src/ephy_worker/ordinary_alias.py", "tests\\test_hidden.py", "tests/../test_hidden.py", "Tests/test_hidden.py"])
def test_review_v13_collection_scope_rejects_new_or_alternate_paths(tmp_path, name):
    from ephy_worker.formal_artifacts import GateFailure
    from ephy_worker.owner_repair_discovery import _CHECKER, preserve_collection_scope
    with pytest.raises(GateFailure, match="new_collection_module_not_allowed"):
        preserve_collection_scope([name], {})
    directory, candidate = tmp_path / "checker", tmp_path / "candidate"
    directory.mkdir()
    candidate.mkdir()
    checker = directory / "checker.py"
    checker.write_text(_CHECKER, encoding="utf-8")
    (directory / "checker-guard.json").write_text(json.dumps({"allowed": [name], "snapshot": {},
        "authority": {}, "tests": {}}), encoding="utf-8")
    marker = tmp_path / "unexpected-acceptance"
    (directory / "acceptance.py").write_text("from pathlib import Path\nPath(" + repr(str(marker)) + ").touch()\n")
    result = subprocess.run([sys.executable, "-I", "-X", "utf8", "-B", str(checker), str(candidate)],
        capture_output=True, check=False, timeout=30)
    assert result.returncode != 0 and b"new_collection_module_not_allowed" in result.stderr
    assert not marker.exists()


def test_review_v13_existing_test_scope_remains_appendable():
    from ephy_worker.owner_repair_discovery import preserve_collection_scope
    preserve_collection_scope(["tests/test_existing.py", "src/ephy_worker/feature.py"],
        {"tests/test_existing.py": {"sha256": "a" * 64}, "src/ephy_worker/feature.py": {"sha256": "b" * 64}})


def test_review_v13_actual_prepare_emits_new_collection_guard(tmp_path, monkeypatch):
    import hashlib
    import shutil

    from ephy_worker.formal_artifacts import GateFailure, file_hash, write_json
    source = tmp_path / "source"
    names = ["src/ephy_worker/feature.py", "tests/test_feature.py"]
    originals = {names[0]: "def answer(value):\n    return value\n",
        names[1]: "def test_original():\n    assert True\n"}
    for name, text in originals.items():
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    observed_snapshot = {name: {"sha256": hashlib.sha256(text.encode()).hexdigest()} for name, text in originals.items()}
    monkeypatch.setattr("ephy_worker.owner_repair_discovery.snapshot", lambda _: observed_snapshot)
    template = tmp_path / "template.json"
    write_json(template, {"runtime": {}, "contract": {}})
    profile = SimpleNamespace(source=source, formal_template=template, formal_template_sha256=file_hash(template),
        allowed_prefixes=("src/", "tests/"), origin="https://github.com/kirimine170/ephy-worker.git")
    discovery = LocalDiscovery(SimpleNamespace(repositories={"ephy-worker": profile}))
    responses = iter([{"repository": "ephy-worker", "allowed_files": names, "semantic_scope": "Correct answer"},
        {"checker_python": "assert True\n", "known_good": {names[0]: "def answer(value):\n    return value + 1\n"},
            "wrong_answer": {names[0]: "def answer(value):\n    return value - 1\n"}}])
    monkeypatch.setattr(discovery, "completion", lambda *_: next(responses))
    monkeypatch.setattr(discovery, "_git", lambda _root, *args: ("\n".join(names) if args[0] == "ls-files"
        else profile.origin if args[0] == "remote" else "" if args[0] == "status" else "d" * 40).encode())
    observed = []
    def controls(_profile, directory, _prepared, checker, _template):
        for ordinal, name in enumerate(("tests/conftest.py", "src/ephy_worker/test_alias.py")):
            candidate = tmp_path / ("specimen-" + str(ordinal))
            shutil.copytree(source, candidate)
            (candidate / name).write_text("import os\nos._exit(0)\n")
            guard_path = directory / "checker-guard.json"
            guard = json.loads(guard_path.read_text())
            guard["allowed"] = [*names, name]
            guard["roles"] = {name: "ordinary_source"}
            guard_path.write_text(json.dumps(guard), encoding="utf-8")
            result = subprocess.run([sys.executable, "-I", "-X", "utf8", "-B", str(checker), str(candidate)],
                capture_output=True, check=False, timeout=30)
            assert result.returncode != 0 and b"new_collection_module_not_allowed" in result.stderr
            observed.append(name)
        raise GateFailure("fixture_emitted_collection_guard_checked")
    monkeypatch.setattr(discovery, "measure_controls", controls)
    with pytest.raises(GateFailure, match="fixture_emitted_collection_guard_checked"):
        discovery.prepare({"text": "synthetic repair", "previous": None}, tmp_path / "discovery")
    assert observed == ["tests/conftest.py", "src/ephy_worker/test_alias.py"]


def _discovery_queue_scope_fixture(tmp_path):
    source = tmp_path / "source"
    names = ["src/ephy_worker/search.py", "tests/test_search.py"]
    for name, text in zip(names, ("def search(query):\n    return query\n", "def test_search():\n    assert 1 == 1\n"), strict=True):
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    profile = SimpleNamespace(source=source, allowed_prefixes=("src/", "tests/"))
    discovery = LocalDiscovery(SimpleNamespace(repositories={"ephy-worker": profile}))
    selected = {"repository": "ephy-worker", "allowed_files": list(names), "semantic_scope": "Improve search behavior"}
    return discovery, {"ephy-worker": {"runtime": {}}}, {"ephy-worker": names}, selected


def test_discovery_queue_empty_then_read_more_then_real_code_selection(tmp_path, monkeypatch):
    discovery, templates, inventories, selected = _discovery_queue_scope_fixture(tmp_path)
    responses = iter([{}, {"read_more": {"ephy-worker": [inventories["ephy-worker"][0]]}}, selected])
    prompts = []
    def complete(prompt, _runtime):
        prompts.append(json.loads(prompt))
        return next(responses)
    monkeypatch.setattr(discovery, "completion", complete)
    directory = tmp_path / "private-diagnostics"
    assert discovery.select_scope({"text": "Improve search", "previous": None}, templates, inventories, directory) == selected
    assert "def search(query)" in prompts[0]["code_context"]["ephy-worker"][inventories["ephy-worker"][0]]["text"]
    assert prompts[1]["previous_selection_error"] == "discovery_scope_empty_or_invalid"
    assert prompts[2]["previous_selection_error"] == "discovery_read_more_requested"
    assert json.loads((directory / "scope-attempt-1.response.json").read_text()) == {}
    assert (directory / "scope-attempt-2.validation.json").is_file()


def test_discovery_queue_malformed_selection_retries_and_context_is_bounded(tmp_path, monkeypatch):
    discovery, templates, inventories, selected = _discovery_queue_scope_fixture(tmp_path)
    for index in range(12):
        name = f"src/ephy_worker/search_{index}.py"
        (discovery.config.repositories["ephy-worker"].source / name).write_text("# search\n" * 9000, encoding="utf-8")
        inventories["ephy-worker"].append(name)
    prompts = []
    def complete(prompt, _runtime):
        prompts.append(json.loads(prompt))
        if len(prompts) == 1:
            raise ValueError("fixture malformed JSON")
        if len(prompts) == 2:
            return {}
        return selected
    monkeypatch.setattr(discovery, "completion", complete)
    directory = tmp_path / "private"
    assert discovery.select_scope({"text": "search", "previous": None}, templates, inventories, directory) == selected
    assert len(prompts) == 3
    assert all(len(json.dumps(prompt["code_context"], ensure_ascii=False).encode()) <= 24000 for prompt in prompts)
    assert json.loads((directory / "scope-attempt-1.error.json").read_text())["type"] == "ValueError"


def test_discovery_queue_exhaustion_retains_responses_and_public_explanation(tmp_path, monkeypatch):
    from ephy_worker.formal_artifacts import GateFailure
    discovery, templates, inventories, _ = _discovery_queue_scope_fixture(tmp_path)
    monkeypatch.setattr(discovery, "completion", lambda *_: {})
    directory = tmp_path / "private"
    with pytest.raises(GateFailure, match="discovery_scope_exhausted"):
        discovery.select_scope({"text": "fixture private directive", "previous": "fixture previous"}, templates, inventories, directory)
    assert len(list(directory.glob("*.response.json"))) == len(list(directory.glob("*.validation.json"))) == 30
    def fail(*_):
        raise GateFailure("discovery_scope_exhausted")
    config = SimpleNamespace(state_dir=tmp_path / "state", guild_id=11, channel_ids=(20,))
    controller = RepairController(config, discovery=SimpleNamespace(prepare=fail), release=SimpleNamespace())
    identifier = controller.store.enqueue(**directive())
    assert controller.tick() and controller.store.get(identifier)["stage"] == "failed"
    reply = "".join(row["text"] for row in controller.store.pending(guild_id=11, channel_ids=(20,)))
    assert "変更するファイルとテストを特定できません" in reply and "具体的" in reply
    assert "fixture private directive" not in reply and "fixture previous" not in reply


@pytest.mark.asyncio
async def test_discovery_queue_owner_retryable_exception_keeps_fifo_head(tmp_path):
    bridge, gateway, original = bridge_fixture(tmp_path)
    event = replace(original, content="<@10> 回答の表示を修正して")
    attempts = 0
    async def classify(text, previous):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("fixture transient transport failure")
        return True
    gateway.classify_repair = classify
    sent = []
    async def send(text):
        sent.append(text)
    await bridge.handle(event, send)
    assert bridge.ledger.inbox_pending() and bridge.repairs.next() is None
    async def recovered(_row, text):
        sent.append(text)
    await bridge.recover_intakes(recovered)
    await bridge.recover_intakes(recovered)
    assert attempts == 2 and bridge.repairs.next() is not None and not bridge.ledger.inbox_pending()
    with bridge.repairs.db() as db:
        assert db.execute("SELECT count(*) FROM jobs").fetchone()[0] == 1
    assert sent.count(RECEIPT) == 1


def test_context_review_unseen_selection_requires_code_and_next_round_confirmation(tmp_path, monkeypatch):
    discovery, templates, inventories, _ = _discovery_queue_scope_fixture(tmp_path)
    source = discovery.config.repositories["ephy-worker"].source
    for index in range(6):
        name = f"src/ephy_worker/search_ranked_{index}.py"
        (source / name).write_text("# search\n" * 1000, encoding="utf-8")
        inventories["ephy-worker"].append(name)
    unseen = "src/ephy_worker/z_feature.py"
    (source / unseen).write_text("def unseen_answer():\n    return 42\n", encoding="utf-8")
    inventories["ephy-worker"].append(unseen)
    selected = {"repository": "ephy-worker", "allowed_files": [unseen, "tests/test_search.py"],
        "semantic_scope": "Improve answer behavior"}
    prompts = []
    def complete(prompt, _runtime):
        prompts.append(json.loads(prompt))
        return selected
    monkeypatch.setattr(discovery, "completion", complete)
    assert discovery.select_scope({"text": "search", "previous": None}, templates, inventories, tmp_path / "private") == selected
    assert len(prompts) == 2 and unseen not in prompts[0]["code_context"]["ephy-worker"]
    assert "def unseen_answer()" in prompts[1]["code_context"]["ephy-worker"][unseen]["text"]
    assert "tests/test_search.py" in prompts[1]["code_context"]["ephy-worker"]
    assert prompts[1]["previous_selection_error"] == "discovery_scope_confirmation_required"
    assert len(json.dumps(prompts[0]["code_context"], ensure_ascii=False).encode("utf-8")) <= 12000


def test_context_review_four_multibyte_read_more_excerpts_survive_next_prompts(tmp_path, monkeypatch):
    discovery, templates, inventories, selected = _discovery_queue_scope_fixture(tmp_path)
    source = discovery.config.repositories["ephy-worker"].source
    requested = []
    for index in range(4):
        name = f"src/ephy_worker/read_extra_{index}.py"
        (source / name).write_text("# 日本語\n" + "日" * 8000, encoding="utf-8")
        inventories["ephy-worker"].append(name)
        requested.append(name)
    for index in range(4):
        name = f"src/ephy_worker/z_later_{index}.py"
        (source / name).write_text("# additional context\n" * 1000, encoding="utf-8")
        inventories["ephy-worker"].append(name)
    responses = iter([{"read_more": {"ephy-worker": requested}}, {}, selected])
    prompts = []
    def complete(prompt, _runtime):
        prompts.append(json.loads(prompt))
        return next(responses)
    monkeypatch.setattr(discovery, "completion", complete)
    assert discovery.select_scope({"text": "search", "previous": None}, templates, inventories, tmp_path / "private") == selected
    for name in requested:
        second = prompts[1]["code_context"]["ephy-worker"][name]
        assert second == prompts[2]["code_context"]["ephy-worker"][name]
        assert second["truncated"] is True and second["utf8_bytes"] == len(second["text"].encode("utf-8"))
        assert second["text"] and "日本語" in second["text"]
    assert all(len(json.dumps(prompt["code_context"], ensure_ascii=False).encode("utf-8")) <= 24000 for prompt in prompts)


def test_context_review_oversized_read_more_is_atomic_and_preserves_accepted_context(tmp_path, monkeypatch):
    from ephy_worker.formal_artifacts import GateFailure
    discovery, templates, inventories, _ = _discovery_queue_scope_fixture(tmp_path)
    source = discovery.config.repositories["ephy-worker"].source
    for index in range(6):
        name = f"src/ephy_worker/search_ranked_{index}.py"
        (source / name).write_text("# search\n" * 1000, encoding="utf-8")
        inventories["ephy-worker"].append(name)
    groups = []
    for group in ("first", "second"):
        names = []
        for index in range(4):
            name = f"src/ephy_worker/z_{group}_{index}.py"
            (source / name).write_text("# 日本語\n" + "日" * 8000, encoding="utf-8")
            inventories["ephy-worker"].append(name)
            names.append(name)
        groups.append(names)
    responses = iter([{"read_more": {"ephy-worker": groups[0]}}, {"read_more": {"ephy-worker": groups[1]}}, {}] + [{}] * 27)
    prompts = []
    def complete(prompt, _runtime):
        prompts.append(json.loads(prompt))
        return next(responses)
    monkeypatch.setattr(discovery, "completion", complete)
    directory = tmp_path / "private"
    with pytest.raises(GateFailure, match="discovery_scope_exhausted"):
        discovery.select_scope({"text": "search", "previous": None}, templates, inventories, directory)
    assert prompts[2]["previous_selection_error"] == "discovery_context_request_too_large"
    assert prompts[1]["code_context"] == prompts[2]["code_context"]
    assert all(name in prompts[2]["code_context"]["ephy-worker"] for name in groups[0])
    assert all(name not in prompts[2]["code_context"]["ephy-worker"] for name in groups[1])
    assert json.loads((directory / "scope-attempt-2.validation.json").read_text())["error"] == "discovery_context_request_too_large"


@pytest.mark.parametrize("content,valid", [('{"selected":true}', True),
    ('```json\n{"selected":true}\n```', False), ('{"selected":true,"selected":false}', False)])
def test_discovery_json_http_request_format_and_strict_raw_evidence(tmp_path, monkeypatch, content, valid):
    identity = tmp_path / "identity.json"
    identity.write_text(json.dumps({"context": 32768, "model_id": "fixture-model"}), encoding="utf-8")
    monkeypatch.setitem(sys.modules, "ephy_worker.existing_llama_runtime", SimpleNamespace(verify_identity=lambda _: None))
    calls = []
    def handler(request):
        body = json.loads(request.content)
        calls.append((request.url.path, body))
        if request.url.path == "/apply-template":
            return httpx.Response(200, json={"prompt": "fixture formatted prompt"})
        if request.url.path == "/tokenize":
            return httpx.Response(200, json={"tokens": [1, 2, 3]})
        assert request.url.path == "/v1/chat/completions"
        assert body["response_format"] == {"type": "json_object"}
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": content}}]})
    actual_client = httpx.Client
    monkeypatch.setattr("ephy_worker.owner_repair_discovery.httpx.Client", lambda **kwargs:
        actual_client(transport=httpx.MockTransport(handler), **kwargs))
    discovery = LocalDiscovery(SimpleNamespace())
    runtime = {"models_ini": str(identity), "base_url": "http://127.0.0.1:18080"}
    directory = tmp_path / "private"
    if valid:
        assert discovery.ask("fixture scope", runtime, directory, "scope") == {"selected": True}
    else:
        with pytest.raises(ValueError):
            discovery.ask("fixture scope", runtime, directory, "scope")
        assert (directory / "scope.error.json").is_file()
    raw = json.loads((directory / "scope.raw.json").read_bytes())
    assert raw["choices"][0]["message"]["content"] == content
    assert calls[-1][1]["messages"] == calls[0][1]["messages"]
    assert calls[-1][1]["max_tokens"] == 32768 - 3 - 64 and calls[-1][1]["stream"] is False


@pytest.mark.parametrize("fault,expected", [("origin", "discovery_origin_changed"),
    ("dirty", "registered_source_not_clean"), ("snapshot", "fixture unsafe snapshot")])
def test_preflight_review_all_registered_sources_checked_before_scope(tmp_path, monkeypatch, fault, expected):
    from ephy_worker.formal_artifacts import GateFailure, file_hash, write_json

    profiles = {}
    for alias in ("ephy-worker", "ephy-runtime"):
        source = tmp_path / alias
        source.mkdir()
        template = tmp_path / (alias + ".json")
        write_json(template, {"runtime": {}})
        profiles[alias] = SimpleNamespace(source=source, formal_template=template,
            formal_template_sha256=file_hash(template), allowed_prefixes=("src/",),
            origin="https://github.com/kirimine170/" + alias + ".git")
    discovery = LocalDiscovery(SimpleNamespace(repositories=profiles))
    checked = []
    def git(root, *args):
        alias = root.name
        checked.append((alias, args[0]))
        if args[0] == "remote":
            return ("https://example.invalid/replacement.git" if alias == "ephy-runtime" and fault == "origin"
                else profiles[alias].origin).encode()
        if args[0] == "status":
            return b" M src/feature.py" if alias == "ephy-runtime" and fault == "dirty" else b""
        assert args == ("ls-files",)
        return b"src/feature.py\n"
    def safe_snapshot(root):
        checked.append((root.name, "snapshot"))
        if root.name == "ephy-runtime" and fault == "snapshot":
            raise GateFailure("fixture unsafe snapshot")
        return {}
    def unexpected(*_):
        pytest.fail("scope selection/inference ran before registered-source preflight")
    monkeypatch.setattr(discovery, "_git", git)
    monkeypatch.setattr("ephy_worker.owner_repair_discovery.snapshot", safe_snapshot)
    monkeypatch.setattr(discovery, "select_scope", unexpected)
    monkeypatch.setattr(discovery, "completion", unexpected)
    with pytest.raises(GateFailure, match=expected):
        discovery.prepare({"text": "Improve fixture feature", "previous": None}, tmp_path / "private")
    assert ("ephy-worker", "snapshot") in checked and ("ephy-runtime", "remote") in checked


def test_preflight_review_direct_scope_rejects_linked_parent_before_read_or_inference(tmp_path, monkeypatch):
    from ephy_worker.formal_artifacts import GateFailure

    source = tmp_path / "source"
    (source / "src").mkdir(parents=True)
    replacement = tmp_path / "replacement"
    replacement.mkdir()
    (replacement / "feature.py").write_text("def fixture_replacement():\n    return 42\n", encoding="utf-8")
    link = source / "src/ephy_worker"
    if os.name == "nt":
        subprocess.run(["cmd.exe", "/c", "mklink", "/J", str(link), str(replacement)],
            capture_output=True, check=True, timeout=10)
    else:
        link.symlink_to(replacement, target_is_directory=True)
    profile = SimpleNamespace(source=source, allowed_prefixes=("src/",))
    discovery = LocalDiscovery(SimpleNamespace(repositories={"ephy-worker": profile}))
    def unexpected(*_):
        pytest.fail("linked replacement reached inference")
    monkeypatch.setattr(discovery, "completion", unexpected)
    with pytest.raises(GateFailure, match="Link/reparse point prohibited"):
        discovery.select_scope({"text": "Improve fixture feature", "previous": None},
            {"ephy-worker": {"runtime": {}}}, {"ephy-worker": ["src/ephy_worker/feature.py"]}, tmp_path / "private")


def test_discovery_loop_recovers_after_three_with_shape_and_missing_test_feedback(tmp_path, monkeypatch):
    discovery, templates, inventories, selected = _discovery_queue_scope_fixture(tmp_path)
    missing_tests = {**selected, "allowed_files": [inventories["ephy-worker"][0]]}
    mixed = {**selected, "read_more": {"ephy-worker": [inventories["ephy-worker"][0]]}}
    responses = iter([{"files": selected["allowed_files"]}, missing_tests, mixed,
        {"read_more": {"ephy-worker": [inventories["ephy-worker"][0]]}}, selected])
    prompts = []
    def complete(prompt, runtime):
        prompts.append(json.loads(prompt))
        assert "oneOf" in runtime["discovery_response_schema"]
        return next(responses)
    monkeypatch.setattr(discovery, "completion", complete)
    job = {"text": "Improve fixture search behavior", "previous": "Fixture prior answer"}
    directory = tmp_path / "private"
    assert discovery.select_scope(job, templates, inventories, directory) == selected
    assert len(prompts) == 5
    assert all(prompt["request"] == job["text"] and prompt["previous_answer"] == job["previous"] for prompt in prompts)
    assert json.loads(prompts[1]["previous_model_output"]["text"]) == {"files": selected["allowed_files"]}
    assert "Do not mix" in prompts[1]["correction"]
    assert prompts[2]["previous_selection_error"] == "discovery_scope_protected_or_missing_tests"
    assert prompts[2]["existing_test_files"]["ephy-worker"] == ["tests/test_search.py"]
    assert "existing tests/ file" in prompts[2]["correction"]
    assert json.loads(prompts[2]["previous_model_output"]["text"]) == missing_tests
    assert prompts[3]["previous_selection_error"] == "discovery_scope_empty_or_invalid"
    outcome = json.loads((directory / "scope-attempt-5.validation.json").read_bytes())
    assert outcome == {"outcome": "selected", "round": 5, "provisional_round_limit": 30}
    assert len(list(directory.glob("*.response.json"))) == len(list(directory.glob("*.validation.json"))) == 5
    assert "discovery_response_schema" not in templates["ephy-worker"]["runtime"]


def test_discovery_loop_parse_feedback_retains_raw_and_bounds_utf8(tmp_path, monkeypatch):
    discovery, templates, inventories, selected = _discovery_queue_scope_fixture(tmp_path)
    prompts = []
    content = "```json\n" + "日本語" * 5000 + "\n```"
    def complete(prompt, _runtime):
        prompts.append(json.loads(prompt))
        if len(prompts) == 1:
            discovery.response_path.write_text(json.dumps({"choices": [{"message": {"content": content}}]},
                ensure_ascii=False), encoding="utf-8")
            raise ValueError("fixture strict JSON parse failure")
        return selected
    monkeypatch.setattr(discovery, "completion", complete)
    directory = tmp_path / "private"
    assert discovery.select_scope({"text": "Improve search", "previous": None}, templates, inventories, directory) == selected
    previous = prompts[1]["previous_model_output"]
    assert previous["representation"] == "raw_http_response" and previous["truncated"] is True
    assert "```json" in previous["text"] and 0 < len(previous["text"].encode("utf-8")) <= 8192
    assert previous["utf8_bytes"] > 8192 and "without fences" in prompts[1]["correction"]
    assert json.loads((directory / "scope-attempt-1.raw.json").read_bytes())["choices"][0]["message"]["content"] == content


@pytest.mark.parametrize("read_more", [False, True])
def test_discovery_loop_http_uses_exclusive_json_schema_for_both_shapes(tmp_path, monkeypatch, read_more):
    from jsonschema import Draft7Validator

    discovery, templates, inventories, selected = _discovery_queue_scope_fixture(tmp_path)
    identity = tmp_path / "identity.json"
    identity.write_text(json.dumps({"context": 32768, "model_id": "fixture-model"}), encoding="utf-8")
    templates["ephy-worker"]["runtime"] = {"models_ini": str(identity), "base_url": "http://127.0.0.1:18080"}
    monkeypatch.setitem(sys.modules, "ephy_worker.existing_llama_runtime", SimpleNamespace(verify_identity=lambda _: None))
    formats = []
    request_more = {"read_more": {"ephy-worker": [inventories["ephy-worker"][0]]}}
    def handler(request):
        body = json.loads(request.content)
        if request.url.path == "/apply-template":
            return httpx.Response(200, json={"prompt": "fixture formatted prompt"})
        if request.url.path == "/tokenize":
            return httpx.Response(200, json={"tokens": [1, 2, 3]})
        assert request.url.path == "/v1/chat/completions"
        formats.append(body["response_format"])
        contract = body["response_format"]
        assert contract["type"] == "json_schema"
        assert contract["json_schema"]["name"] == "discovery_scope" and contract["json_schema"]["strict"] is True
        validator = Draft7Validator(contract["json_schema"]["schema"])
        assert validator.is_valid(selected) and validator.is_valid(request_more)
        assert not validator.is_valid({**selected, **request_more})
        assert not validator.is_valid({k: v for k, v in selected.items() if k != "semantic_scope"})
        assert not validator.is_valid({"read_more": {"unregistered": ["src/feature.py"]}})
        response = request_more if read_more and len(formats) == 1 else selected
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(response)}}]})
    actual_client = httpx.Client
    monkeypatch.setattr("ephy_worker.owner_repair_discovery.httpx.Client", lambda **kwargs:
        actual_client(transport=httpx.MockTransport(handler), **kwargs))
    assert discovery.select_scope({"text": "Improve search", "previous": None}, templates, inventories, tmp_path / "private") == selected
    assert len(formats) == (2 if read_more else 1)


@pytest.mark.parametrize("context,input_tokens,expected_output", [(32768, 16175, 16529),
    (4260, 100, 4096), (4259, 100, None)])
def test_discovery_generation_uses_remaining_context_and_minimum_headroom(tmp_path, monkeypatch,
        context, input_tokens, expected_output):
    from ephy_worker.formal_artifacts import GateFailure

    identity = tmp_path / "identity.json"
    identity.write_text(json.dumps({"context": context, "model_id": "fixture-model"}), encoding="utf-8")
    monkeypatch.setitem(sys.modules, "ephy_worker.existing_llama_runtime", SimpleNamespace(verify_identity=lambda _: None))
    calls = []
    def handler(request):
        data = json.loads(request.content)
        calls.append((request.url.path, data))
        if request.url.path == "/apply-template":
            return httpx.Response(200, json={"prompt": "measured formatted fixture"})
        if request.url.path == "/tokenize":
            assert data == {"content": "measured formatted fixture", "add_special": False}
            return httpx.Response(200, json={"tokens": [1] * input_tokens})
        assert request.url.path == "/v1/chat/completions"
        assert data["max_tokens"] == expected_output
        assert input_tokens + data["max_tokens"] + 64 == context
        assert data["messages"] == calls[0][1]["messages"]
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": '{"fixture":true}'}}]})
    actual_client = httpx.Client
    monkeypatch.setattr("ephy_worker.owner_repair_discovery.httpx.Client", lambda **kwargs:
        actual_client(transport=httpx.MockTransport(handler), **kwargs))
    discovery = LocalDiscovery(SimpleNamespace())
    runtime = {"models_ini": str(identity), "base_url": "http://127.0.0.1:18080"}
    if expected_output is None:
        with pytest.raises(GateFailure, match="context_budget_exceeded"):
            discovery.completion("original private fixture request", runtime)
        assert [path for path, _ in calls] == ["/apply-template", "/tokenize"]
    else:
        assert discovery.completion("original private fixture request", runtime) == {"fixture": True}
        assert calls[-1][1]["max_tokens"] == expected_output


@pytest.mark.parametrize("finish_reason,content,error", [("length", '{"fixture":true}', RuntimeError),
    ("stop", '{"fixture":', ValueError)])
def test_discovery_generation_rejects_incomplete_and_preserves_raw_response(tmp_path, monkeypatch, finish_reason, content, error):
    identity = tmp_path / "identity.json"
    identity.write_text(json.dumps({"context": 32768, "model_id": "fixture-model"}), encoding="utf-8")
    monkeypatch.setitem(sys.modules, "ephy_worker.existing_llama_runtime", SimpleNamespace(verify_identity=lambda _: None))
    def handler(request):
        if request.url.path == "/apply-template":
            return httpx.Response(200, json={"prompt": "measured fixture prompt"})
        if request.url.path == "/tokenize":
            return httpx.Response(200, json={"tokens": [1, 2, 3]})
        assert request.url.path == "/v1/chat/completions"
        assert json.loads(request.content)["max_tokens"] == 32768 - 3 - 64
        return httpx.Response(200, json={"choices": [{"finish_reason": finish_reason, "message": {"content": content}}]})
    actual_client = httpx.Client
    monkeypatch.setattr("ephy_worker.owner_repair_discovery.httpx.Client", lambda **kwargs:
        actual_client(transport=httpx.MockTransport(handler), **kwargs))
    discovery = LocalDiscovery(SimpleNamespace())
    directory = tmp_path / "private"
    with pytest.raises(error, match="generation_incomplete" if finish_reason == "length" else None):
        discovery.ask("fixture request", {"models_ini": str(identity), "base_url": "http://127.0.0.1:18080"}, directory, "controls")
    raw = json.loads((directory / "controls.raw.json").read_bytes())
    assert raw["choices"][0] == {"finish_reason": finish_reason, "message": {"content": content}}
    assert (directory / "controls.error.json").is_file()
