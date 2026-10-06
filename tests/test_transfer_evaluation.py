"""Synthetic transfer controls; no model invocation or transfer-effect claim."""

from __future__ import annotations

import contextlib
import ctypes
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import ephy_worker.transfer_evaluation as transfer
from ephy_worker.coding_profiles import load_coding_profiles
from ephy_worker.evaluation import create_fixture_repository, load_suite
from ephy_worker.transfer_evaluation import (
    ASSETS,
    TransferSuite,
    comparison_plan,
    fixture_repository,
    freeze_transfer_suite,
    main,
    run_controls,
    validate_candidate,
    variant_manifest,
)

ENV = {"platform": "synthetic-linux", "python": "3.12", "dependencies": "lock-v1", "executor": "planned"}
BUDGET = {"timeout_seconds": 10, "max_turns": 8, "max_tool_calls": 16}


class TransferEvaluationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        original = freeze_transfer_suite()
        self.assets = self.root / "assets"
        self.assets.mkdir()
        for name, data in original.assets:
            (self.assets / name).write_bytes(data)
        self.frozen = freeze_transfer_suite(self.assets)
        self.profile = load_coding_profiles()["mock"]

    def candidate(self, good=True):
        task = self.frozen.suite.tasks[0]
        repository, _ = self.enterContext(fixture_repository(task.fixture))
        if good:
            for change in task.fixture.mock_changes:
                (repository / change.path).write_text(change.content)
        return task.fixture.task_id, repository

    def test_fixture_cleanup_removes_readonly_git_objects(self):
        task = self.frozen.suite.tasks[0]
        with fixture_repository(task.fixture) as (repository, _):
            objects = list((repository / ".git" / "objects").glob("??/*"))
            self.assertTrue(objects)
            for path in objects:
                path.chmod(path.stat().st_mode & ~stat.S_IWRITE)
                metadata = path.stat()
                if os.name == "nt":
                    self.assertTrue(metadata.st_file_attributes & stat.FILE_ATTRIBUTE_READONLY)
                else:
                    self.assertFalse(metadata.st_mode & stat.S_IWRITE)
        self.assertFalse(repository.exists())

    def test_fixture_cleanup_accepts_windows_short_path_alias(self):
        task = self.frozen.suite.tasks[0]

        def create_alias_fixture(fixture):
            repository, revision = create_fixture_repository(fixture)
            alias = repository.parent / ".." / repository.parent.name / repository.name
            if os.name == "nt":
                get_short_path = ctypes.WinDLL("kernel32", use_last_error=True).GetShortPathNameW
                get_short_path.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32]
                get_short_path.restype = ctypes.c_uint32
                size = get_short_path(str(repository), None, 0)
                if not size:
                    raise ctypes.WinError(ctypes.get_last_error())
                buffer = ctypes.create_unicode_buffer(size)
                if not get_short_path(str(repository), buffer, size):
                    raise ctypes.WinError(ctypes.get_last_error())
                short_path = Path(buffer.value)
                if short_path != repository.resolve(strict=True):
                    alias = short_path
            return alias, revision

        with (
            patch(
                "ephy_worker.transfer_evaluation.create_fixture_repository",
                side_effect=create_alias_fixture,
            ),
            fixture_repository(task.fixture) as (repository, _),
        ):
            self.assertNotEqual(repository, repository.resolve(strict=True))
            objects = list((repository / ".git" / "objects").glob("??/*"))
            self.assertTrue(objects)
            for path in objects:
                path.chmod(path.stat().st_mode & ~stat.S_IWRITE)
        self.assertFalse(repository.exists())

    def test_fixture_cleanup_propagates_unrelated_permission_errors(self):
        task = self.frozen.suite.tasks[0]
        for relative, readonly in (("note.txt", True), (".git/objects/aa/" + "b" * 38, False)):
            with self.subTest(relative=relative):
                repository = self.root / ("readonly" if readonly else "writable")
                target = repository / relative
                target.parent.mkdir(parents=True)
                target.write_text("keep", encoding="utf-8")
                if readonly:
                    target.chmod(target.stat().st_mode & ~stat.S_IWRITE)
                error = PermissionError("unrelated cleanup failure")

                def fail_cleanup(path, *, onexc, target=target, error=error):
                    onexc(os.unlink, target, error)

                try:
                    with (
                        patch(
                            "ephy_worker.transfer_evaluation.create_fixture_repository",
                            return_value=(repository, "unused"),
                        ),
                        patch("ephy_worker.transfer_evaluation.shutil.rmtree", side_effect=fail_cleanup),
                        self.assertRaises(PermissionError) as raised,
                        fixture_repository(task.fixture),
                    ):
                        pass
                    self.assertIs(raised.exception, error)
                    self.assertEqual(target.read_text(encoding="utf-8"), "keep")
                finally:
                    target.chmod(target.stat().st_mode | stat.S_IWRITE)

    def test_fixture_cleanup_rejects_link_escape(self):
        task = self.frozen.suite.tasks[0]
        repository = self.root / "allocation"
        repository.mkdir()
        outside = self.root / "outside"
        outside.mkdir()
        marker = outside / "keep.txt"
        marker.write_text("keep", encoding="utf-8")
        try:
            with (
                patch(
                    "ephy_worker.transfer_evaluation.create_fixture_repository",
                    return_value=(repository, "unused"),
                ),
                self.assertRaisesRegex(OSError, "not the allocated directory"),
                fixture_repository(task.fixture),
            ):
                repository.rmdir()
                repository.symlink_to(outside, target_is_directory=True)
            self.assertEqual(marker.read_text(encoding="utf-8"), "keep")
        finally:
            if repository.is_symlink():
                repository.unlink()

    def mutate_git_metadata(self, repository, mutation):
        git = repository / ".git"
        target = git / "config"
        if mutation == "hook":
            target = git / "hooks" / "pre-commit"
            target.write_text("# unrelated hook\n", encoding="utf-8")
        elif mutation == "config":
            target.write_bytes(target.read_bytes() + b"\n# unrelated config\n")
        elif mutation == "delete_file":
            target = git / "HEAD"
            target.unlink()
        elif mutation == "add_directory":
            target = git / "empty-extra"
            target.mkdir()
        elif mutation == "readonly":
            target.chmod(target.stat().st_mode & ~stat.S_IWRITE)
        elif mutation == "replace_file":
            replacement = git / "replacement"
            replacement.write_bytes(target.read_bytes())
            replacement.replace(target)
        elif mutation == "file_directory":
            target.unlink()
            target.mkdir()
        elif mutation in {"replace_directory", "delete_directory", "root_file"}:
            target = git
            saved = self.root / (repository.name + "-saved-git")
            git.rename(saved)
            if mutation == "replace_directory":
                shutil.copytree(saved, git)
            elif mutation == "root_file":
                git.write_text("replacement", encoding="utf-8")
        else:
            raise ValueError(mutation)
        return target.relative_to(repository).as_posix()

    def test_git_metadata_mutations_fail_scope_before_checker(self):
        for mutation in (
            "hook",
            "config",
            "delete_file",
            "add_directory",
            "readonly",
            "replace_file",
            "file_directory",
            "replace_directory",
            "delete_directory",
            "root_file",
        ):
            with self.subTest(mutation=mutation):
                task_id, repository = self.candidate()
                good = validate_candidate(self.frozen, task_id, repository)
                self.assertTrue(good["passed"])
                target = self.mutate_git_metadata(repository, mutation)
                try:
                    with patch("ephy_worker.transfer_evaluation.subprocess.run") as run:
                        verdict = validate_candidate(self.frozen, task_id, repository)
                    run.assert_not_called()
                    self.assertFalse(verdict["passed"])
                    self.assertEqual(verdict["status"], "scope_failure")
                    self.assertIn(target, verdict["outside_scope"])
                    self.assertNotEqual(good["candidate_sha256"], verdict["candidate_sha256"])
                finally:
                    if mutation == "readonly":
                        path = repository / target
                        path.chmod(path.stat().st_mode | stat.S_IWRITE)

    def test_git_metadata_mutations_during_checker_fail_integrity(self):
        real_run = subprocess.run
        for mutation in ("hook", "config", "delete_file", "add_directory", "replace_file", "root_file"):
            with self.subTest(mutation=mutation):
                task_id, repository = self.candidate()

                def mutate(command, *, repository=repository, mutation=mutation, **kwargs):
                    result = real_run(command, **kwargs)
                    self.mutate_git_metadata(repository, mutation)
                    return result

                with patch("ephy_worker.transfer_evaluation.subprocess.run", side_effect=mutate):
                    verdict = validate_candidate(self.frozen, task_id, repository)
                self.assertTrue(verdict["correctness_passed"])
                self.assertFalse(verdict["candidate_unchanged"])
                self.assertFalse(verdict["passed"])
                self.assertEqual(verdict["status"], "integrity_failure")

    def test_unknown_git_baseline_including_empty_directory_fails_closed(self):
        task = self.frozen.suite.tasks[0]
        for with_config in (False, True):
            with self.subTest(with_config=with_config):
                repository = self.root / ("external-config" if with_config else "external-empty")
                for relative, content in task.fixture.files.items():
                    target = repository / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(content, encoding="utf-8")
                for change in task.fixture.mock_changes:
                    (repository / change.path).write_text(change.content, encoding="utf-8")
                (repository / ".git").mkdir()
                if with_config:
                    (repository / ".git" / "config").write_text("config", encoding="utf-8")
                with patch("ephy_worker.transfer_evaluation.subprocess.run") as run:
                    verdict = validate_candidate(self.frozen, task.fixture.task_id, repository)
                run.assert_not_called()
                self.assertFalse(verdict["passed"])
                self.assertIn(".git", verdict["outside_scope"])

    def test_git_baseline_is_bound_to_the_fixture_identity(self):
        _, repository = self.candidate()
        original = self.frozen.suite.tasks[0]
        other = self.frozen.suite.tasks[1]
        for relative in original.fixture.files:
            (repository / relative).unlink()
        for relative, content in other.fixture.files.items():
            target = repository / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        for change in other.fixture.mock_changes:
            (repository / change.path).write_text(change.content, encoding="utf-8")
        with patch("ephy_worker.transfer_evaluation.subprocess.run") as run:
            verdict = validate_candidate(self.frozen, other.fixture.task_id, repository)
        run.assert_not_called()
        self.assertFalse(verdict["passed"])
        self.assertIn(".git", verdict["outside_scope"])

    def test_git_baseline_is_bound_to_the_owned_root_allocation(self):
        task = self.frozen.suite.tasks[0]
        with fixture_repository(task.fixture) as (repository, _):
            for change in task.fixture.mock_changes:
                (repository / change.path).write_text(change.content, encoding="utf-8")
            original_identity = (repository.stat().st_dev, repository.stat().st_ino)
            saved = self.root / "original-allocation"
            repository.rename(saved)
            try:
                shutil.copytree(saved, repository)
                self.assertNotEqual(original_identity, (repository.stat().st_dev, repository.stat().st_ino))
                with patch("ephy_worker.transfer_evaluation.subprocess.run") as run:
                    verdict = validate_candidate(self.frozen, task.fixture.task_id, repository)
                run.assert_not_called()
                self.assertFalse(verdict["passed"])
                self.assertIn(".git", verdict["outside_scope"])
            finally:
                if repository.exists():
                    repository.rename(self.root / "replacement-allocation")
                saved.rename(repository)

    def test_git_links_rejected_without_reading_foreign_bytes(self):
        foreign = self.root / "foreign.txt"
        foreign.write_text("must not read", encoding="utf-8")
        real_read = Path.read_bytes
        for kind in ("symbolic", "hardlink"):
            with self.subTest(kind=kind):
                task_id, repository = self.candidate()
                target = repository / ".git" / "config"
                target.unlink()
                if kind == "symbolic":
                    target.symlink_to(foreign)
                else:
                    target.hardlink_to(foreign)

                def no_foreign_read(path, *, target=target):
                    if path == target:
                        self.fail("read foreign bytes through Git metadata")
                    return real_read(path)

                with (
                    patch.object(Path, "read_bytes", no_foreign_read),
                    patch("ephy_worker.transfer_evaluation.subprocess.run") as run,
                    self.assertRaisesRegex(ValueError, "symbolic link|hardlinked"),
                ):
                    validate_candidate(self.frozen, task_id, repository)
                run.assert_not_called()
                target.unlink()
        self.assertEqual(foreign.read_text(encoding="utf-8"), "must not read")

    def test_git_reparse_point_rejected_before_directory_traversal(self):
        task_id, repository = self.candidate()
        target = repository / ".git" / "objects"
        real_lstat = Path.lstat
        real_iterdir = Path.iterdir

        def reparse_metadata(path):
            metadata = real_lstat(path)
            if path == target:
                return SimpleNamespace(
                    st_mode=metadata.st_mode,
                    st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT,
                )
            return metadata

        def no_reparse_traversal(path):
            if path == target:
                self.fail("traversed a Git reparse point")
            return real_iterdir(path)

        with (
            patch.object(Path, "lstat", reparse_metadata),
            patch.object(Path, "iterdir", no_reparse_traversal),
            self.assertRaisesRegex(ValueError, "reparse point"),
        ):
            validate_candidate(self.frozen, task_id, repository)

    def test_git_structural_change_during_checker_is_integrity_failure(self):
        task_id, repository = self.candidate()
        foreign = self.root / "foreign-after.txt"
        foreign.write_text("keep", encoding="utf-8")
        real_run = subprocess.run

        def mutate(command, **kwargs):
            result = real_run(command, **kwargs)
            target = repository / ".git" / "config"
            target.unlink()
            target.symlink_to(foreign)
            return result

        with patch("ephy_worker.transfer_evaluation.subprocess.run", side_effect=mutate):
            verdict = validate_candidate(self.frozen, task_id, repository)
        self.assertTrue(verdict["correctness_passed"])
        self.assertFalse(verdict["candidate_unchanged"])
        self.assertFalse(verdict["passed"])
        self.assertEqual(verdict["status"], "integrity_failure")
        self.assertIn("symbolic link", verdict["candidate_error"])
        self.assertEqual(foreign.read_text(encoding="utf-8"), "keep")

    def test_git_postcheck_filesystem_errors_propagate(self):
        task_id, repository = self.candidate()
        target = repository / ".git" / "config"
        checking_finished = False
        real_run = subprocess.run
        real_read = Path.read_bytes
        error = PermissionError("unexpected filesystem failure")

        def finish(command, **kwargs):
            nonlocal checking_finished
            result = real_run(command, **kwargs)
            checking_finished = True
            return result

        def fail_read(path):
            if checking_finished and path == target:
                raise error
            return real_read(path)

        with (
            patch("ephy_worker.transfer_evaluation.subprocess.run", side_effect=finish),
            patch.object(Path, "read_bytes", fail_read),
            self.assertRaises(PermissionError) as raised,
        ):
            validate_candidate(self.frozen, task_id, repository)
        self.assertIs(raised.exception, error)

    def test_git_baseline_is_immutable_and_active_allocation_cannot_refresh(self):
        task = self.frozen.suite.tasks[0]
        with fixture_repository(task.fixture) as (repository, revision):
            key = repository.resolve(strict=True)
            baseline = transfer._FIXTURE_GIT_BASELINES[key]
            with self.assertRaises(TypeError):
                baseline.entries[".git"] = baseline.entries[".git"]
            with (
                patch(
                    "ephy_worker.transfer_evaluation.create_fixture_repository",
                    return_value=(repository, revision),
                ),
                self.assertRaisesRegex(ValueError, "already has an active"),
                fixture_repository(task.fixture),
            ):
                self.fail("refreshed an active allocation")
            self.assertIs(transfer._FIXTURE_GIT_BASELINES[key], baseline)
            self.assertTrue(repository.is_dir())
        self.assertNotIn(key, transfer._FIXTURE_GIT_BASELINES)
        self.assertFalse(repository.exists())

    def test_git_baseline_removed_when_capture_or_yield_fails(self):
        task = self.frozen.suite.tasks[0]
        with (
            self.assertRaisesRegex(RuntimeError, "caller failure"),
            fixture_repository(task.fixture) as (repository, _),
        ):
            key = repository.resolve(strict=True)
            raise RuntimeError("caller failure")
        self.assertNotIn(key, transfer._FIXTURE_GIT_BASELINES)
        self.assertFalse(repository.exists())
        repository = self.root / "invalid-capture"
        target = repository / ".git" / "config"
        target.parent.mkdir(parents=True)
        foreign = self.root / "capture-foreign"
        foreign.write_text("keep", encoding="utf-8")
        target.symlink_to(foreign)
        key = repository.resolve(strict=True)
        real_read = Path.read_bytes

        def no_foreign_read(path):
            if path == target:
                self.fail("read foreign bytes while capturing baseline")
            return real_read(path)

        with (
            patch(
                "ephy_worker.transfer_evaluation.create_fixture_repository",
                return_value=(repository, "unused"),
            ),
            patch.object(Path, "read_bytes", no_foreign_read),
            self.assertRaisesRegex(ValueError, "symbolic link"),
            fixture_repository(task.fixture),
        ):
            self.fail("accepted an invalid metadata baseline")
        self.assertNotIn(key, transfer._FIXTURE_GIT_BASELINES)
        self.assertFalse(repository.exists())
        self.assertEqual(foreign.read_text(encoding="utf-8"), "keep")

    def test_root_replacement_with_identical_children_during_checker_fails_integrity(self):
        task_id, repository = self.candidate()
        baseline = validate_candidate(self.frozen, task_id, repository)
        self.assertTrue(baseline["passed"])
        before = transfer._candidate_files(repository)
        saved = self.root / "original-root"
        real_run = subprocess.run

        def replace_root(command, **kwargs):
            result = real_run(command, **kwargs)
            repository.rename(saved)
            repository.mkdir()
            for child in list(saved.iterdir()):
                child.rename(repository / child.name)
            return result

        try:
            with patch("ephy_worker.transfer_evaluation.subprocess.run", side_effect=replace_root):
                verdict = validate_candidate(self.frozen, task_id, repository)
            after = transfer._candidate_files(repository)
            self.assertEqual(
                {path: entry for path, entry in before.items() if path != "."},
                {path: entry for path, entry in after.items() if path != "."},
            )
            self.assertNotEqual(before["."].identity, after["."].identity)
            self.assertNotEqual(transfer._candidate_digest(before), transfer._candidate_digest(after))
            self.assertTrue(verdict["scope_passed"])
            self.assertTrue(verdict["correctness_passed"])
            self.assertFalse(verdict["candidate_unchanged"])
            self.assertFalse(verdict["passed"])
            self.assertEqual(verdict["status"], "integrity_failure")
            with patch("ephy_worker.transfer_evaluation.subprocess.run") as run:
                rejected = validate_candidate(self.frozen, task_id, repository)
            run.assert_not_called()
            self.assertFalse(rejected["passed"])
            self.assertIn(".", rejected["outside_scope"])
            self.assertNotEqual(baseline["candidate_sha256"], rejected["candidate_sha256"])
        finally:
            if saved.exists():
                for child in list(repository.iterdir()):
                    child.rename(saved / child.name)
                repository.rmdir()
                saved.rename(repository)
        self.assertEqual(before["."].identity, transfer._candidate_files(repository)["."].identity)

    def test_root_permissions_fail_scope_and_bind_candidate_hash(self):
        task_id, repository = self.candidate()
        baseline = validate_candidate(self.frozen, task_id, repository)
        self.assertTrue(baseline["passed"])
        mode = repository.stat().st_mode
        try:
            repository.chmod(mode & ~stat.S_IWRITE)
            with patch("ephy_worker.transfer_evaluation.subprocess.run") as run:
                verdict = validate_candidate(self.frozen, task_id, repository)
            run.assert_not_called()
            self.assertFalse(verdict["passed"])
            self.assertEqual(verdict["status"], "scope_failure")
            self.assertIn(".", verdict["outside_scope"])
            self.assertNotEqual(baseline["candidate_sha256"], verdict["candidate_sha256"])
        finally:
            repository.chmod(mode)

    def test_root_permissions_during_checker_fail_integrity(self):
        task_id, repository = self.candidate()
        mode = repository.stat().st_mode
        real_run = subprocess.run

        def mutate(command, **kwargs):
            result = real_run(command, **kwargs)
            repository.chmod(mode & ~stat.S_IWRITE)
            return result

        try:
            with patch("ephy_worker.transfer_evaluation.subprocess.run", side_effect=mutate):
                verdict = validate_candidate(self.frozen, task_id, repository)
            self.assertTrue(verdict["correctness_passed"])
            self.assertFalse(verdict["candidate_unchanged"])
            self.assertFalse(verdict["passed"])
            self.assertEqual(verdict["status"], "integrity_failure")
        finally:
            repository.chmod(mode)

    def test_root_removal_during_checker_fails_integrity(self):
        task_id, repository = self.candidate()
        saved = self.root / "removed-root"
        real_run = subprocess.run

        def remove_root(command, **kwargs):
            result = real_run(command, **kwargs)
            repository.rename(saved)
            return result

        try:
            with patch("ephy_worker.transfer_evaluation.subprocess.run", side_effect=remove_root):
                verdict = validate_candidate(self.frozen, task_id, repository)
            self.assertTrue(verdict["correctness_passed"])
            self.assertFalse(verdict["candidate_unchanged"])
            self.assertFalse(verdict["passed"])
            self.assertEqual(verdict["status"], "integrity_failure")
            self.assertIn("real directory", verdict["candidate_error"])
        finally:
            if saved.exists():
                saved.rename(repository)

    def test_root_link_and_file_rejected_before_descendant_traversal(self):
        task_id, repository = self.candidate()
        alias = self.root / "root-link"
        alias.symlink_to(repository, target_is_directory=True)
        file = self.root / "root-file"
        file.write_text("must not read", encoding="utf-8")
        real_iterdir = Path.iterdir

        def no_traversal(path):
            if path in (alias, file):
                self.fail("traversed invalid candidate root")
            return real_iterdir(path)

        for root in (alias, file):
            with (
                self.subTest(root=root.name),
                patch.object(Path, "iterdir", no_traversal),
                patch("ephy_worker.transfer_evaluation.subprocess.run") as run,
                self.assertRaisesRegex(ValueError, "real directory|symbolic link"),
            ):
                validate_candidate(self.frozen, task_id, root)
            run.assert_not_called()

    def test_root_reparse_point_rejected_before_descendant_traversal(self):
        task_id, repository = self.candidate()
        real_lstat = Path.lstat
        real_iterdir = Path.iterdir

        def reparse_metadata(path):
            metadata = real_lstat(path)
            if path == repository:
                return SimpleNamespace(
                    st_mode=metadata.st_mode, st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT
                )
            return metadata

        def no_traversal(path):
            if path == repository:
                self.fail("traversed candidate root reparse point")
            return real_iterdir(path)

        with (
            patch.object(Path, "lstat", reparse_metadata),
            patch.object(Path, "iterdir", no_traversal),
            patch("ephy_worker.transfer_evaluation.subprocess.run") as run,
            self.assertRaisesRegex(ValueError, "reparse point"),
        ):
            validate_candidate(self.frozen, task_id, repository)
        run.assert_not_called()

    def test_unmanaged_candidate_root_is_outside_scope_even_without_git(self):
        task = self.frozen.suite.tasks[0]
        repository = self.root / "unmanaged-root"
        for relative, content in task.fixture.files.items():
            target = repository / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        for change in task.fixture.mock_changes:
            (repository / change.path).write_text(change.content, encoding="utf-8")
        with patch("ephy_worker.transfer_evaluation.subprocess.run") as run:
            verdict = validate_candidate(self.frozen, task.fixture.task_id, repository)
        run.assert_not_called()
        self.assertFalse(verdict["passed"])
        self.assertEqual(verdict["status"], "scope_failure")
        self.assertIn(".", verdict["outside_scope"])

    def plan(self, **changes):
        args = {
            "split": "development",
            "profile": self.profile,
            "environment": ENV,
            "budget": BUDGET,
            "repeat": 2,
        }
        args.update(changes)
        return comparison_plan(self.frozen, **args)

    def test_six_distinct_tasks_have_disjoint_splits(self):
        suite = self.frozen.suite
        development = {t.fixture.task_id for t in suite.tasks if t.split == "development"}
        heldout = {t.fixture.task_id for t in suite.tasks if t.split == "heldout"}
        self.assertEqual((len(development), len(heldout)), (3, 3))
        self.assertFalse(development & heldout)
        self.assertFalse((development | heldout) & {t.task_id for t in load_suite("smoke").tasks})
        self.assertEqual(
            {t.family for t in suite.tasks if t.split == "development"},
            {t.family for t in suite.tasks if t.split == "heldout"},
        )

    def test_all_thirty_known_controls(self):
        report = run_controls(self.frozen)
        self.assertTrue(report["passed"])
        self.assertEqual(len(report["controls"]), 30)
        self.assertEqual(report["transfer_effect"], "unmeasured")
        self.assertEqual(report["formal_workflow"], "unverified")
        for row in report["controls"]:
            self.assertEqual(row["verdict"]["passed"], row["control"] == "known_good")

    def test_frozen_asset_mutation_rejected_before_execution(self):
        for name in ASSETS:
            path = self.assets / name
            before = path.read_bytes()
            path.write_bytes(before + b"\n")
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "frozen asset"):
                self.frozen.verify()
            path.write_bytes(before)

    def test_frozen_asset_removal_and_symlink_rejected(self):
        path = self.assets / "checker.py"
        data = path.read_bytes()
        path.unlink()
        with self.assertRaisesRegex(ValueError, "frozen asset"):
            self.frozen.verify()
        elsewhere = self.root / "replacement.py"
        elsewhere.write_bytes(data)
        path.symlink_to(elsewhere)
        with self.assertRaisesRegex(ValueError, "frozen asset"):
            self.frozen.verify()
        with self.assertRaisesRegex(ValueError, "symbolic"):
            freeze_transfer_suite(self.assets)

    def test_in_memory_mutations_do_not_change_frozen_suite(self):
        suite = self.frozen.suite
        suite.tasks[0].allowed_files.append("tests/test_behavior.py")
        self.assertEqual(len(self.frozen.suite.tasks[0].allowed_files), 1)

    def test_balanced_plan_freezes_all_nonvariant_conditions(self):
        plan = self.plan()
        self.assertEqual(plan["status"], "plan_only")
        self.assertEqual(len(plan["runs"]), 18)
        for key in ("suite_sha256", "checker_sha256", "model_sha256", "budget_sha256", "environment_sha256"):
            self.assertEqual(len({row[key] for row in plan["runs"]}), 1)
        self.assertEqual({row["variant_label"] for row in plan["runs"]}, {"none", "skill", "tool"})
        self.assertEqual([row["variant_label"] for row in plan["runs"][:3]], ["none", "skill", "tool"])
        self.assertEqual([row["variant_label"] for row in plan["runs"][9:12]], ["skill", "tool", "none"])
        self.assertEqual(len({row["variant_sha256"] for row in plan["runs"]}), 3)

    def test_model_requests_exclude_solutions_checks_and_other_split(self):
        plan = self.plan()
        self.assertNotIn("heldout-", json.dumps(plan))
        for row in plan["runs"]:
            request = row["request"]
            self.assertNotIn("mock_changes", request)
            self.assertNotIn("checker", request)
            self.assertFalse(any(p.startswith("tests/") for p in request["files"]))
            task = self.frozen.task(row["task_id"])
            for change in task.fixture.mock_changes:
                self.assertNotIn(change.content, request["files"].values())
        heldout = self.plan(split="heldout")
        self.assertTrue(all(row["split"] == "heldout" for row in heldout["runs"]))
        for variant in ("none", "skill", "tool"):
            before = next(row["variant"] for row in plan["runs"] if row["variant_label"] == variant)
            after = next(row["variant"] for row in heldout["runs"] if row["variant_label"] == variant)
            self.assertEqual(before, after)

    def test_invalid_plan_and_unknown_tasks_fail_closed(self):
        for changes in (
            {"split": "all"},
            {"repeat": 0},
            {"repeat": True},
            {"environment": {}},
            {"budget": {"timeout_seconds": 99}},
            {"budget": {"timeout_seconds": True}},
            {"budget": {"timeout_seconds": 10, "tokens": float("nan")}},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.plan(**changes)
        with self.assertRaises(ValueError):
            self.frozen.task("unknown")
        with self.assertRaises(ValueError):
            variant_manifest(self.frozen, "both")

    def test_duplicate_split_and_out_of_scope_good_control_rejected(self):
        raw = self.frozen.suite.model_dump(mode="json")
        raw["tasks"][3]["fixture"]["task_id"] = raw["tasks"][0]["fixture"]["task_id"]
        with self.assertRaises(ValueError):
            TransferSuite.model_validate(raw)
        raw = self.frozen.suite.model_dump(mode="json")
        raw["tasks"][0]["allowed_files"] = ["tests/test_behavior.py"]
        with self.assertRaises(ValueError):
            TransferSuite.model_validate(raw)

    def test_early_zero_exit_cannot_pass_fixed_checker(self):
        task_id, repository = self.candidate()
        (repository / "batches.py").write_text("import os\nos._exit(0)\n")
        verdict = validate_candidate(self.frozen, task_id, repository)
        self.assertEqual(verdict["exit_code"], 0)
        self.assertFalse(verdict["passed"])

    def test_candidate_modification_during_check_rejected(self):
        task_id, repository = self.candidate()
        path = repository / "batches.py"
        path.write_text(
            path.read_text() + "\nfrom pathlib import Path\nPath(__file__).write_text('# mutated\\n')\n"
        )
        verdict = validate_candidate(self.frozen, task_id, repository)
        self.assertTrue(verdict["correctness_passed"])
        self.assertFalse(verdict["candidate_unchanged"])
        self.assertEqual(verdict["status"], "integrity_failure")

    def test_checker_copy_modification_during_check_rejected(self):
        task_id, repository = self.candidate()
        real_run = subprocess.run

        def mutate(command, **kwargs):
            result = real_run(command, **kwargs)
            Path(command[3]).write_text("# checker replaced\n")
            return result

        with patch("ephy_worker.transfer_evaluation.subprocess.run", side_effect=mutate):
            verdict = validate_candidate(self.frozen, task_id, repository)
        self.assertFalse(verdict["checker_unchanged"])
        self.assertFalse(verdict["passed"])

    @unittest.skipUnless(os.name == "posix", "requires Unix executable mode bits")
    def test_mode_only_out_of_scope_change_is_rejected_before_execution(self):
        task_id, repository = self.candidate()
        path = repository / "tests/test_behavior.py"
        original = path.read_bytes()
        path.chmod(path.stat().st_mode | 0o111)
        self.assertEqual(path.read_bytes(), original)
        git_changes = subprocess.check_output(["git", "diff", "--summary"], cwd=repository, text=True)
        self.assertIn("mode change", git_changes)
        with patch("ephy_worker.transfer_evaluation.subprocess.run") as run:
            verdict = validate_candidate(self.frozen, task_id, repository)
        run.assert_not_called()
        self.assertFalse(verdict["passed"])
        self.assertIn("tests/test_behavior.py", verdict["outside_scope"])

    @unittest.skipUnless(os.name == "posix", "requires Unix executable mode bits")
    def test_mode_change_during_validation_breaks_candidate_integrity(self):
        task_id, repository = self.candidate()
        real_run = subprocess.run

        def mutate(command, **kwargs):
            result = real_run(command, **kwargs)
            path = repository / "batches.py"
            path.chmod(path.stat().st_mode | 0o111)
            return result

        with patch("ephy_worker.transfer_evaluation.subprocess.run", side_effect=mutate):
            verdict = validate_candidate(self.frozen, task_id, repository)
        self.assertTrue(verdict["correctness_passed"])
        self.assertFalse(verdict["candidate_unchanged"])
        self.assertFalse(verdict["passed"])
        self.assertEqual(verdict["status"], "integrity_failure")

    @unittest.skipUnless(os.name == "posix", "requires Unix executable mode bits")
    def test_executable_bit_migration_during_validation_is_detected(self):
        task_id, repository = self.candidate()
        path = repository / "batches.py"
        path.chmod(0o744)
        baseline = validate_candidate(self.frozen, task_id, repository)
        real_run = subprocess.run

        def mutate(command, **kwargs):
            result = real_run(command, **kwargs)
            path.chmod(0o645)
            return result

        with patch("ephy_worker.transfer_evaluation.subprocess.run", side_effect=mutate):
            verdict = validate_candidate(self.frozen, task_id, repository)
        self.assertTrue(baseline["passed"])
        self.assertTrue(verdict["correctness_passed"])
        self.assertFalse(verdict["candidate_unchanged"])
        self.assertFalse(verdict["passed"])
        after = validate_candidate(self.frozen, task_id, repository)
        self.assertNotEqual(baseline["candidate_sha256"], after["candidate_sha256"])

    @unittest.skipUnless(os.name == "posix", "requires Unix executable mode bits")
    def test_candidate_digest_binds_executable_state(self):
        task_id, repository = self.candidate()
        before = validate_candidate(self.frozen, task_id, repository)
        path = repository / "batches.py"
        path.chmod(path.stat().st_mode | 0o111)
        after = validate_candidate(self.frozen, task_id, repository)
        self.assertTrue(before["passed"])
        self.assertTrue(after["passed"])
        self.assertNotEqual(before["candidate_sha256"], after["candidate_sha256"])

    def test_scope_checked_before_fixed_test_exec(self):
        task_id, repository = self.candidate()
        (repository / "tests/test_behavior.py").write_text("raise RuntimeError('must never run')")
        with patch("ephy_worker.transfer_evaluation.subprocess.run") as run:
            verdict = validate_candidate(self.frozen, task_id, repository)
        run.assert_not_called()
        self.assertEqual(verdict["status"], "scope_failure")
        self.assertFalse(verdict["passed"])

    def test_symlink_and_ignored_untracked_candidate_rejected(self):
        task_id, repository = self.candidate()
        (repository / "cache.pyc").write_bytes(b"extra")
        verdict = validate_candidate(self.frozen, task_id, repository)
        self.assertFalse(verdict["passed"])
        (repository / "cache.pyc").unlink()
        (repository / "batches.py").unlink()
        (repository / "batches.py").symlink_to(self.assets / "checker.py")
        with self.assertRaisesRegex(ValueError, "symbolic link"):
            validate_candidate(self.frozen, task_id, repository)

    def test_timeout_and_environment_errors_are_not_pass(self):
        task_id, repository = self.candidate()
        for error, expected in (
            (subprocess.TimeoutExpired("check", 1), "execution_timeout"),
            (OSError("missing"), "environment_failure"),
        ):
            with patch("ephy_worker.transfer_evaluation.subprocess.run", side_effect=error):
                verdict = validate_candidate(self.frozen, task_id, repository)
            self.assertFalse(verdict["passed"])
            self.assertEqual(verdict["status"], expected)

    def test_tool_only_reads_supplied_source(self):
        source = "def f(x):\n    if x > 0:\n        return x\n"
        result = subprocess.run(
            [sys.executable, "-I", "-B", str(self.assets / "inspect_python.py")],
            input=json.dumps({"source": source}),
            capture_output=True,
            text=True,
            check=True,
        )
        payload = json.loads(result.stdout)
        self.assertEqual(payload["functions"][0]["name"], "f")
        self.assertEqual(payload["comparisons"][0]["expression"], "x > 0")
        invalid = subprocess.run(
            [sys.executable, "-I", "-B", str(self.assets / "inspect_python.py")],
            input=json.dumps({"path": "/etc/passwd"}),
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(invalid.returncode, 0)

    def test_tool_accepts_escaped_sources_at_decoded_byte_limit(self):
        # JSON escaping expands the envelope, not the decoded source budget.
        sources = ["#" + "\\" * 65535, "#" + "あ" * 21845, "#" + "a" * 65535]
        for source in sources:
            self.assertEqual(len(source.encode("utf-8")), 65536)
            encoded = json.dumps({"source": source}, ensure_ascii=True)
            if source.endswith("a"):
                encoded = '{"source":"' + "".join(f"\\u{ord(c):04x}" for c in source) + '"}'
            self.assertGreater(len(encoded), 131073)
            with self.subTest(source_kind=source[-1]):
                result = subprocess.run(
                    [sys.executable, "-I", "-B", str(self.assets / "inspect_python.py")],
                    input=encoded,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(
                    json.loads(result.stdout), {"functions": [], "branches": [], "comparisons": []}
                )

    def test_tool_rejects_oversized_source_and_envelope_without_truncating(self):
        cases = [
            (json.dumps({"source": "#" + "a" * 65536}), "at most 65536 bytes"),
            (json.dumps({"source": "#" + "あ" * 21846}), "at most 65536 bytes"),
            (json.dumps({"source": "#"}) + " " * (1 << 20), "JSON envelope exceeds"),
        ]
        for encoded, error in cases:
            with self.subTest(error=error):
                result = subprocess.run(
                    [sys.executable, "-I", "-B", str(self.assets / "inspect_python.py")],
                    input=encoded,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(error, result.stderr)
                self.assertEqual(result.stdout, "")

    def test_lookup_rejects_ignored_key_and_mutated_stored_value(self):
        task = self.frozen.task("heldout-present-value")
        wrong_implementations = [
            (
                "def present_value(mapping, key, fallback):\n"
                "    return next(iter(mapping.values())) if mapping else fallback\n"
            ),
            (
                "def present_value(mapping, key, fallback):\n"
                "    value = mapping[key] if key in mapping else fallback\n"
                "    if isinstance(value, list):\n"
                "        value.append(99)\n"
                "    return value\n"
            ),
        ]
        for source in wrong_implementations:
            with self.subTest(source=source), fixture_repository(task.fixture) as (repository, _):
                (repository / "lookup.py").write_text(source)
                verdict = validate_candidate(self.frozen, task.fixture.task_id, repository)
                self.assertFalse(verdict["passed"])
                self.assertFalse(verdict["correctness_passed"])

    def test_cli_controls_report_is_synthetic(self):
        with (
            patch(
                "ephy_worker.transfer_evaluation.run_controls",
                return_value={"passed": True, "evidence_kind": "synthetic"},
            ),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(main(["controls"]), 0)
        self.assertEqual(json.loads(output.getvalue())["evidence_kind"], "synthetic")


if __name__ == "__main__":
    unittest.main()
