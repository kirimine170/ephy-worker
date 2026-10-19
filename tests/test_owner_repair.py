"""Authorization, durability and transport routing without live services．"""

import ast
import asyncio
import json
import os
import subprocess
import sys
import threading
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
        return {"exit_code": 0 if kwargs["candidate"].name == "known_good" else 1}
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
    started, finish = threading.Event(), threading.Event()
    def previous(_):
        started.set()
        assert finish.wait(5)
    monkeypatch.setattr(bridge.ledger, "previous", previous)
    sent = []
    async def send(text):
        sent.append(text)
    handler = asyncio.create_task(bridge.handle(event, send))
    assert await asyncio.to_thread(started.wait, 5)
    (bridge.settings.state_dir / "repair-drain").write_text("drain-after-await")
    finish.set()
    await handler
    assert gateway.classifications == [] and gateway.answers == [] and sent == []
    assert bridge.repairs.next() is None
    state = json.loads((bridge.settings.state_dir / "repair-active.json").read_text())
    assert state["active"] == 0 and state["drain_token"] == "drain-after-await"


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
        assert calls[-1][1]["max_tokens"] == 4096
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
        calls.append((argv, kwargs))
        result = subprocess.run(argv, cwd=kwargs["cwd"], env=kwargs["environment"],
            capture_output=True, check=False, timeout=30)
        assert result.stdout.decode("utf-8").strip() == "日本語の検証結果"
        assert not result.stderr
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
