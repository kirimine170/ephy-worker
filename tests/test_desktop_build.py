"""Pinned build-input validation without running PyInstaller or a native app."""

import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "build_desktop.ps1"


def validation_source():
    text = SCRIPT.read_text(encoding="utf-8")
    match = re.search(r"\$officialAppValidationScript = @'\n(.*?)\n'@", text, re.DOTALL)
    assert match is not None
    return match.group(1)


@pytest.fixture
def validator(tmp_path):
    source = tmp_path / "trusted_build_validation.py"
    source.write_text(validation_source(), encoding="utf-8")
    spec = importlib.util.spec_from_file_location("_desktop_build_input_tests", source)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return vars(module)


@pytest.fixture
def synthetic_bundle(tmp_path, validator):
    root = tmp_path / "synthetic-public-app"
    root.mkdir()
    entries = []
    for number in range(20):
        name = "karte.exe" if number == 0 else f"resources/item-{number:02d}.txt"
        raw = f"SYNTHETIC app resource {number}\n".encode()
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        entries.append({"path": name, "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()})
    manifest = tmp_path / "synthetic-manifest.json"
    manifest.write_text(json.dumps(entries), encoding="utf-8")
    # The fixture supplies a tiny isolated inventory to the same production checks.
    # The actual build's immutable manifest pin is never modified on disk.
    validator["MANIFEST_SHA256"] = hashlib.sha256(manifest.read_bytes()).hexdigest()
    provenance = tmp_path / "provenance.json"
    provenance.write_text('{"synthetic":true}', encoding="utf-8")
    return root, entries, manifest, provenance


def test_exact_app_files_pass_and_leave_inputs_unchanged(validator, synthetic_bundle):
    root, entries, manifest, _ = synthetic_bundle
    before = {item["path"]: (root / item["path"]).read_bytes() for item in entries}
    assert validator["read_manifest"](manifest) == entries
    validator["validate_tree"](root, entries)
    assert {name: (root / name).read_bytes() for name in before} == before


@pytest.mark.parametrize("mutation", ["changed", "truncated", "missing", "extra", "extra-directory"])
def test_app_content_or_path_set_changes_are_rejected(validator, synthetic_bundle, mutation):
    root, entries, _, _ = synthetic_bundle
    target = root / "karte.exe"
    if mutation == "changed":
        raw = target.read_bytes()
        target.write_bytes(b"X" + raw[1:])
    elif mutation == "truncated":
        target.write_bytes(b"")
    elif mutation == "missing":
        target.unlink()
    elif mutation == "extra":
        (root / "credential.json").write_text("SYNTHETIC forbidden extra", encoding="utf-8")
    else:
        (root / "unexpected").mkdir()
    with pytest.raises(ValueError, match="identity|missing|Unexpected"):
        validator["validate_tree"](root, entries)


def test_changed_manifest_cannot_reauthorize_changed_resource(validator, synthetic_bundle):
    root, entries, manifest, _ = synthetic_bundle
    raw = b"SYNTHETIC replacement"
    (root / "karte.exe").write_bytes(raw)
    entries[0] = {"path": "karte.exe", "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
    manifest.write_text(json.dumps(entries), encoding="utf-8")
    with pytest.raises(ValueError, match="inventory identity"):
        validator["read_manifest"](manifest)


@pytest.mark.parametrize("path", ["../escape", "/absolute", "C:/escape", "resources\\escape", "./karte.exe"])
def test_manifest_path_escape_rejected_before_file_access(validator, synthetic_bundle, path):
    root, entries, _, _ = synthetic_bundle
    entries[0] = {**entries[0], "path": path}
    with pytest.raises(ValueError, match="inventory path"):
        validator["validate_tree"](root, entries)


def test_case_colliding_manifest_paths_rejected(validator, synthetic_bundle):
    root, entries, _, _ = synthetic_bundle
    entries[1] = {**entries[0], "path": "KARTE.EXE"}
    with pytest.raises(ValueError, match="Duplicate"):
        validator["validate_tree"](root, entries)


def test_hardlinked_payload_is_rejected(validator, synthetic_bundle, tmp_path):
    root, entries, _, _ = synthetic_bundle
    os.link(root / "karte.exe", tmp_path / "unowned-hardlink")
    with pytest.raises(ValueError, match="hardlinked"):
        validator["validate_tree"](root, entries)


def test_reparse_directory_is_rejected_without_following_it(validator, synthetic_bundle, monkeypatch):
    root, entries, _, _ = synthetic_bundle
    target = root / "resources"
    original = Path.lstat

    def lstat(path):
        value = original(path)
        if path == target:
            return SimpleNamespace(st_mode=value.st_mode, st_file_attributes=0x400)
        return value

    monkeypatch.setattr(Path, "lstat", lstat)
    with pytest.raises(ValueError, match="reparse"):
        validator["validate_tree"](root, entries)


def test_content_change_during_hashing_is_rejected(validator, synthetic_bundle, monkeypatch):
    root, entries, _, _ = synthetic_bundle
    original = hashlib.file_digest

    def digest(stream, algorithm):
        value = original(stream, algorithm)
        if Path(stream.name) == root / "karte.exe":
            (root / "karte.exe").write_bytes(b"SYNTHETIC concurrent change")
        return value

    monkeypatch.setattr(hashlib, "file_digest", digest)
    with pytest.raises(ValueError, match="changed while hashing"):
        validator["validate_tree"](root, entries)


def test_optimized_interpreter_cannot_bypass_inherited_assert_checks(tmp_path):
    result = subprocess.run(
        [sys.executable, "-B", "-O", "-c", validation_source()],
        cwd=tmp_path, capture_output=True, text=True, timeout=15, check=False,
    )
    assert result.returncode != 0
    assert "Python optimization must be disabled" in result.stderr


def test_preflight_binds_exact_provenance_and_rechecks_copied_payload(
    validator, synthetic_bundle, monkeypatch, tmp_path,
):
    root, entries, manifest, provenance = synthetic_bundle
    calls = []
    module = ModuleType("ephy_worker.desktop.native_karte")

    def validate(app, sidecar):
        calls.append((app, sidecar))
        return {"synthetic": True}

    module.validate_official_app = validate
    monkeypatch.setitem(sys.modules, module.__name__, module)
    monkeypatch.setattr(sys, "path", list(sys.path))
    arguments = [str(tmp_path), str(root), str(manifest), str(provenance)]
    before = validator["main"](arguments)
    assert before["file_count"] == 20
    assert before["provenance_sha256"] == hashlib.sha256(provenance.read_bytes()).hexdigest()
    assert calls == [(root, provenance)]
    frozen = tmp_path / "frozen-app"
    shutil.copytree(root, frozen)
    validator["validate_tree"](frozen, entries)
    (frozen / "karte.exe").write_bytes(b"SYNTHETIC changed packaged copy")
    with pytest.raises(ValueError, match="identity"):
        validator["validate_tree"](frozen, entries)


def test_invalid_payload_stops_before_native_provenance_import(validator, synthetic_bundle, tmp_path):
    root, _, manifest, provenance = synthetic_bundle
    (root / "karte.exe").write_bytes(b"SYNTHETIC invalid")
    with pytest.raises(ValueError, match="identity"):
        validator["main"]([str(tmp_path), str(root), str(manifest), str(provenance)])
