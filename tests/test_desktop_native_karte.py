"""Native launch confinement and current-authorization contracts, with no real app."""

import hashlib
import json
import os
from dataclasses import replace
from unittest.mock import Mock

import pytest

from ephy_worker.desktop import native_karte as native
from ephy_worker.desktop.config import DesktopConfig
from ephy_worker.desktop.inspection import InspectionError


def snapshot(task_id="task-one", body=b"---\ndoc_id: doc-one\n---\nSynthetic only\n"):
    return {"task_id": task_id, "doc_id": "doc-one",
            "relative_path": f"content/projects/project/note/worklog-{task_id}.md",
            "source_sha256": "a" * 64, "markdown_sha256": hashlib.sha256(body).hexdigest(),
            "renderer_commit": "30e5dfbab03f92e2eb7c8a67e17936579075fe88", "markdown": body}


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    app = tmp_path / "bundle" / "app"
    files = {"karte.exe": b"synthetic executable never run", "runtime.dll": b"synthetic runtime",
             "karte_data_template/content/README.md": b"Synthetic template"}
    inventory = {}
    for name, body in files.items():
        target = app / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
        inventory[name] = (len(body), hashlib.sha256(body).hexdigest())
    provenance = {"schema_version": 1, "executable_sha256": inventory["karte.exe"][1]}
    source = app.parent / "provenance.json"
    source.write_text(json.dumps(provenance), encoding="utf-8")
    monkeypatch.setattr(native, "OFFICIAL_FILES", inventory)
    monkeypatch.setattr(native, "OFFICIAL_PROVENANCE", provenance)
    monkeypatch.setattr(native, "_bundle_paths", lambda: (app, source))
    process = Mock(spec_set=["pid", "poll", "terminate", "kill", "wait"], pid=12345)
    process.poll.return_value = None
    spawn = Mock(return_value=process)
    monkeypatch.setattr(native.subprocess, "Popen", spawn)
    renderer = Mock(spec_set=["snapshot"])
    renderer.snapshot.side_effect = lambda task: snapshot(task)
    monkeypatch.setattr(native, "KarteRenderer", Mock(return_value=renderer))
    config = DesktopConfig(repository_path=tmp_path / "repository", artifact_root=tmp_path / "private",
                           karte_root=tmp_path / "production", karte_binary=tmp_path / "writer.exe")
    return config, app, source, renderer, spawn, process


def test_official_pin_includes_actual_native_app_and_distributed_notices():
    assert len(native.OFFICIAL_FILES) == 20
    assert native.OFFICIAL_FILES["karte.exe"] == (
        25823744, "8cfda3d6cdac43a3ef962dfea7858baf308490640fafae327a743d32dd671a38")
    assert native.OFFICIAL_PROVENANCE["commit"] == native.NATIVE_COMMIT
    assert native.OFFICIAL_PROVENANCE["asset_sha256"] == (
        "6f4431e9d18eb5908f1f950e1a81b74724fd9f0f72f23a72a7c6b9ac58108532")
    assert len(native.OFFICIAL_PROVENANCE["notices"]) == 3


def test_prepare_is_read_only_and_shared_single_flight(fixture):
    config, _, _, renderer, spawn, _ = fixture
    launcher = native.NativeKarteLauncher()
    offer = launcher.prepare(config, "task-one")
    assert offer.task_id == "task-one"
    assert launcher.busy and not launcher.active
    assert not config.artifact_root.exists()
    assert not config.karte_root.exists()
    spawn.assert_not_called()
    with pytest.raises(InspectionError, match="native_karte_busy"):
        launcher.prepare(config)
    renderer.snapshot.assert_called_once_with("task-one")
    launcher.discard(offer)
    assert not launcher.busy
    with pytest.raises(InspectionError, match="native_karte_busy"):
        launcher.launch(offer)
    spawn.assert_not_called()


def test_launch_reauthorizes_exact_copy_and_isolates_child_without_mutating_parent(fixture):
    config, app, _, renderer, spawn, process = fixture
    original_environment = dict(os.environ)
    launcher = native.NativeKarteLauncher()
    offer = launcher.prepare(config, "task-one")
    result = launcher.launch(offer)
    assert renderer.snapshot.call_count == 2
    assert result.pid == 12345 and result.relative_path == snapshot()["relative_path"]
    assert result.session_root.parent == config.artifact_root / "karte-desktop"
    assert (result.data_root / result.relative_path).read_bytes() == snapshot()["markdown"]
    assert json.loads((result.data_root / "data/asr/config.json").read_text()) == {"enabled": False}
    receipt = json.loads((result.session_root / "snapshot-receipt.json").read_text())
    assert receipt["kind"] == "authorized-derived-markdown-copy"
    assert receipt["source_sha256"] == snapshot()["source_sha256"]
    assert "markdown" not in receipt and "karte_root" not in receipt
    args, options = spawn.call_args
    assert args == ([str(app / "karte.exe")],)
    assert options["shell"] is False and options["cwd"] == result.session_root
    environment = options["env"]
    assert environment["KARTE_DATA_DIR"] == str(result.data_root)
    for variable, folder in (("APPDATA", "appdata"), ("LOCALAPPDATA", "localappdata"),
                             ("TEMP", "temp"), ("TMP", "temp")):
        assert environment[variable] == str(result.session_root / folder)
    assert options["close_fds"] is True
    assert os.environ == original_environment
    assert not config.karte_root.exists()
    assert not launcher.busy and launcher.active
    launcher.discard(offer)
    process.terminate.assert_not_called()
    process.kill.assert_not_called()
    process.wait.assert_not_called()
    with pytest.raises(InspectionError, match="native_karte_busy"):
        launcher.prepare(config)
    assert spawn.call_count == 1


@pytest.mark.parametrize("field,replacement", [
    ("source_sha256", "b" * 64), ("doc_id", "doc-two"),
    ("relative_path", "content/projects/other/note/worklog-task-one.md"),
    ("renderer_commit", "b" * 40),
])
def test_identity_change_before_commit_creates_no_snapshot(fixture, field, replacement):
    config, _, _, renderer, spawn, _ = fixture
    launcher = native.NativeKarteLauncher()
    offer = launcher.prepare(config, "task-one")
    changed = {**snapshot(), field: replacement}
    renderer.snapshot.side_effect = None
    renderer.snapshot.return_value = changed
    with pytest.raises(InspectionError, match="native_karte_changed"):
        launcher.launch(offer)
    assert not config.artifact_root.exists() and not launcher.busy
    spawn.assert_not_called()


@pytest.mark.parametrize("code", ["karte_renderer_permission_denied", "karte_renderer_v2_conflict",
                                 "karte_renderer_view_conflict", "karte_renderer_view_missing"])
def test_reauthorization_failure_never_copies_or_spawns(fixture, code):
    config, _, _, renderer, spawn, _ = fixture
    launcher = native.NativeKarteLauncher()
    offer = launcher.prepare(config, "task-one")
    renderer.snapshot.side_effect = InspectionError(code)
    with pytest.raises(InspectionError, match=code):
        launcher.launch(offer)
    assert not config.artifact_root.exists() and not launcher.busy
    spawn.assert_not_called()


def test_changed_markdown_even_with_correct_new_hash_rejects_stale_offer(fixture):
    config, _, _, renderer, spawn, _ = fixture
    launcher = native.NativeKarteLauncher()
    offer = launcher.prepare(config, "task-one")
    renderer.snapshot.side_effect = lambda task: snapshot(task, b"Changed synthetic document")
    with pytest.raises(InspectionError, match="native_karte_changed"):
        launcher.launch(offer)
    assert not config.artifact_root.exists()
    spawn.assert_not_called()


def test_offer_expires_after_final_authorization_without_writes(fixture, monkeypatch):
    config, _, _, renderer, spawn, _ = fixture
    clock = [100.0]
    monkeypatch.setattr(native.time, "monotonic", lambda: clock[0])
    launcher = native.NativeKarteLauncher()
    offer = launcher.prepare(config, "task-one")

    def late(task):
        clock[0] += native.NATIVE_OFFER_TTL_SECONDS
        return snapshot(task)

    renderer.snapshot.side_effect = late
    with pytest.raises(InspectionError, match="native_karte_expired"):
        launcher.launch(offer)
    assert not config.artifact_root.exists()
    spawn.assert_not_called()


def test_forged_offer_cannot_consume_current_reservation(fixture):
    config, _, _, _, spawn, _ = fixture
    launcher = native.NativeKarteLauncher()
    offer = launcher.prepare(config)
    forged = replace(offer, task_id="different")
    launcher.discard(forged)
    with pytest.raises(InspectionError, match="native_karte_busy"):
        launcher.launch(forged)
    assert launcher.busy and not config.artifact_root.exists()
    spawn.assert_not_called()
    launcher.discard(offer)


@pytest.mark.parametrize("mutation", ["extra", "missing", "tamper", "empty-directory", "provenance"])
def test_official_payload_changes_fail_closed(fixture, mutation):
    config, app, source, _, spawn, _ = fixture
    if mutation == "extra":
        (app / "unexpected.dll").write_bytes(b"extra")
    elif mutation == "missing":
        (app / "runtime.dll").unlink()
    elif mutation == "tamper":
        (app / "runtime.dll").write_bytes(b"x" * (app / "runtime.dll").stat().st_size)
    elif mutation == "empty-directory":
        (app / "unexpected").mkdir()
    else:
        source.write_text('{"schema_version":1,"extra":"untrusted"}', encoding="utf-8")
    launcher = native.NativeKarteLauncher()
    with pytest.raises(InspectionError, match="native_karte_assets"):
        launcher.prepare(config)
    assert not launcher.busy and not config.artifact_root.exists()
    spawn.assert_not_called()


def test_tamper_after_prepare_is_rechecked_before_any_copy(fixture):
    config, app, _, _, spawn, _ = fixture
    launcher = native.NativeKarteLauncher()
    offer = launcher.prepare(config)
    (app / "karte.exe").write_bytes(b"changed")
    with pytest.raises(InspectionError, match="native_karte_assets"):
        launcher.launch(offer)
    assert not config.artifact_root.exists()
    spawn.assert_not_called()


@pytest.mark.parametrize("location", ["source", "source-child", "asset-child"])
def test_destination_overlap_with_source_or_assets_is_rejected(fixture, location):
    config, app, _, _, spawn, _ = fixture
    root = {"source": config.karte_root.parent, "source-child": config.karte_root,
            "asset-child": app}[location]
    if location == "source":
        config = replace(config, karte_root=root / "karte-desktop")
    config = replace(config, artifact_root=root)
    with pytest.raises(InspectionError, match="native_karte_root"):
        native.NativeKarteLauncher().prepare(config)
    spawn.assert_not_called()


def test_fake_reparse_ancestor_is_rejected_before_creation(fixture, monkeypatch):
    config, _, _, _, spawn, _ = fixture
    config.artifact_root.mkdir()
    config = replace(config, artifact_root=config.artifact_root / "inner")
    original = native._plain

    def deny(path, *, directory):
        if path == config.artifact_root.parent:
            raise OSError("reparse")
        return original(path, directory=directory)

    monkeypatch.setattr(native, "_plain", deny)
    with pytest.raises(InspectionError, match="native_karte_root"):
        native.NativeKarteLauncher().prepare(config)
    assert not config.artifact_root.exists()
    spawn.assert_not_called()


def test_session_collision_never_overwrites_existing_data(fixture, monkeypatch):
    config, _, _, _, spawn, _ = fixture
    occupied = config.artifact_root / "karte-desktop" / ("a" * 32)
    occupied.mkdir(parents=True)
    preserved = occupied / "keep.txt"
    preserved.write_bytes(b"User copy")
    monkeypatch.setattr(native.secrets, "token_hex", lambda _size: "a" * 32)
    launcher = native.NativeKarteLauncher()
    offer = launcher.prepare(config)
    with pytest.raises(InspectionError, match="native_karte_root"):
        launcher.launch(offer)
    assert list(occupied.iterdir()) == [preserved] and preserved.read_bytes() == b"User copy"
    spawn.assert_not_called()


def test_spawn_failure_retains_copy_and_later_launch_uses_new_session(fixture):
    config, _, _, _, spawn, process = fixture
    launcher = native.NativeKarteLauncher()
    offer = launcher.prepare(config)
    spawn.side_effect = OSError("PRIVATE COMMAND PATH")
    with pytest.raises(InspectionError, match="native_karte_launch"):
        launcher.launch(offer)
    first = set((config.artifact_root / "karte-desktop").iterdir())
    assert len(first) == 1 and not launcher.busy and not launcher.active
    spawn.side_effect = None
    spawn.return_value = process
    result = launcher.launch(launcher.prepare(config))
    assert result.session_root not in first
    assert first.issubset(set((config.artifact_root / "karte-desktop").iterdir()))
    assert "PRIVATE" not in native.native_error_text("native_karte_launch")


def test_empty_launch_works_without_source_config_and_never_reads_records(fixture, monkeypatch):
    config, _, _, renderer, _, process = fixture
    monkeypatch.setattr(native, "application_data", lambda: config.artifact_root)
    launcher = native.NativeKarteLauncher()
    result = launcher.launch(launcher.prepare(None))
    assert result.relative_path is None
    assert result.session_root.parent == config.artifact_root / "runs" / "karte-desktop"
    renderer.snapshot.assert_not_called()
    process.poll.return_value = 0
    assert not launcher.active
    other = launcher.launch(launcher.prepare(None))
    assert other.session_root != result.session_root
    assert result.session_root.exists()


def test_low_space_refuses_without_creating_destination(fixture, monkeypatch):
    config, _, _, _, spawn, _ = fixture
    monkeypatch.setattr(native.shutil, "disk_usage", Mock(return_value=Mock(free=0)))
    with pytest.raises(InspectionError, match="native_karte_space"):
        native.NativeKarteLauncher().prepare(config)
    assert not config.artifact_root.exists()
    spawn.assert_not_called()


def test_provenance_integer_does_not_accept_boolean_alias(fixture):
    config, _, source, _, spawn, _ = fixture
    value = json.loads(source.read_text(encoding="utf-8"))
    value["schema_version"] = True
    source.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(InspectionError, match="native_karte_assets"):
        native.NativeKarteLauncher().prepare(config)
    assert not config.artifact_root.exists()
    spawn.assert_not_called()


def test_offer_expired_before_launch_does_not_reauthorize_or_copy(fixture, monkeypatch):
    config, _, _, renderer, spawn, _ = fixture
    clock = [100.0]
    monkeypatch.setattr(native.time, "monotonic", lambda: clock[0])
    launcher = native.NativeKarteLauncher()
    offer = launcher.prepare(config, "task-one")
    clock[0] += native.NATIVE_OFFER_TTL_SECONDS
    with pytest.raises(InspectionError, match="native_karte_expired"):
        launcher.launch(offer)
    renderer.snapshot.assert_called_once_with("task-one")
    assert not config.artifact_root.exists()
    spawn.assert_not_called()


def test_rechecked_bytes_with_wrong_hash_fail_at_write_boundary(fixture):
    config, _, _, renderer, spawn, _ = fixture
    launcher = native.NativeKarteLauncher()
    offer = launcher.prepare(config, "task-one")
    renderer.snapshot.side_effect = None
    renderer.snapshot.return_value = {**snapshot(), "markdown": b"different bytes"}
    with pytest.raises(InspectionError, match="native_karte_changed"):
        launcher.launch(offer)
    assert not config.artifact_root.exists()
    spawn.assert_not_called()
