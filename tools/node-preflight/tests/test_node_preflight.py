"""Deterministic stdlib tests; OS mocks are not real-platform certification."""
import ast
import copy
import ctypes
import hashlib
import io
import json
import os
import tempfile
import unittest
from collections import namedtuple
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import Mock, mock_open, patch

import node_preflight as n


def good_observations():
    obs = {key: n.observation("not_checked", reason="not_requested") for key in n.OBSERVATION_TYPES}
    values = {
        "os": "Linux", "architecture": "x86_64", "python_version": [3, 12, 14],
        "cpu_logical": 8, "cpu_affinity": 8, "memory_total_bytes": 16*n.GIB,
        "memory_available_bytes": 8*n.GIB, "workspace_directory": True,
        "workspace_access_hint": True, "disk_total_bytes": 100*n.GIB,
        "disk_free_bytes": 20*n.GIB, "source_reference_match": True,
    }
    values.update({f"tool.{name}": True for name in n.TOOLS})
    values.update({f"package.{name}": ".".join(map(str, minimum)) for name, (minimum, _) in n.PACKAGES.items()})
    obs.update({key: n.observed(value, "sample") for key, value in values.items()})
    return obs


def sample_json(obs=None):
    return json.dumps({"schema_version": n.SAMPLE_VERSION, "observations": obs or good_observations()})


class SampleTests(unittest.TestCase):
    def load_text(self, content):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sample.json"
            path.write_text(content, encoding="utf-8")
            return n.load_sample(path)

    def test_good_roundtrip(self):
        self.assertEqual(self.load_text(sample_json()), good_observations())

    def test_reject_duplicate_keys(self):
        with self.assertRaisesRegex(n.InputError, "duplicate_json_key"):
            self.load_text('{"schema_version":"x","schema_version":"y"}')

    def test_reject_nonfinite(self):
        for constant in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(constant=constant), self.assertRaises(n.InputError):
                self.load_text('{"observations":' + constant + '}')

    def test_reject_oversize(self):
        with self.assertRaisesRegex(n.InputError, "input_too_large"):
            self.load_text(" " * (n.MAX_INPUT_BYTES + 1))

    def test_reject_extra_missing_keys(self):
        for change in ("missing", "extra"):
            obs = good_observations()
            if change == "missing":
                del obs["tool.git"]
            else:
                obs["credential"] = n.observed("never print me")
            with self.subTest(change=change), self.assertRaisesRegex(n.InputError, "invalid_observation_keys"):
                self.load_text(sample_json(obs))

    def test_reject_bool_as_integer_and_bad_versions(self):
        for key, bad in (("cpu_logical", True), ("disk_free_bytes", -1), ("python_version", [3, True, 0]), ("package.pydantic", "2.11rc1"), ("os", [])):
            obs = good_observations()
            obs[key]["value"] = bad
            with self.subTest(key=key), self.assertRaises(n.InputError):
                n.validate_observations(obs)

    def test_nonobserved_values_must_be_null(self):
        obs = good_observations()
        obs["tool.git"]["status"] = "unknown"
        with self.assertRaisesRegex(n.InputError, "nonobserved_value_must_be_null"):
            n.validate_observations(obs)

    def test_reject_arbitrary_reason(self):
        obs = good_observations()
        obs["tool.git"]["reason"] = "secret/path"
        with self.assertRaisesRegex(n.InputError, "invalid_observation_reason"):
            n.validate_observations(obs)

    def test_reject_impossible_available_or_disk(self):
        for key in ("memory_available_bytes", "disk_free_bytes"):
            obs = good_observations()
            obs[key]["value"] = 1000 * n.GIB
            with self.subTest(key=key), self.assertRaisesRegex(n.InputError, "inconsistent_measurements"):
                n.validate_observations(obs)

    def test_reject_nested_json_and_wrong_schema(self):
        for data in ('[' * 2000 + '0' + ']' * 2000, '{"schema_version":"v0","observations":{}}'):
            with self.subTest(length=len(data)), self.assertRaises(n.InputError):
                self.load_text(data)

    def test_reject_symlink_and_fifo(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            target = path / "target.json"
            target.write_text(sample_json())
            link = path / "link.json"
            try:
                link.symlink_to(target)
            except (OSError, NotImplementedError):
                pass  # Windows without symlink privilege: remaining FIFO/platform checks still run.
            else:
                with self.assertRaises(n.InputError):
                    n.load_sample(link)
            if hasattr(os, "mkfifo"):
                fifo = path / "fifo.json"
                os.mkfifo(fifo)
                with self.assertRaisesRegex(n.InputError, "input_not_regular_file"):
                    n.load_sample(fifo)


class ProfileTests(unittest.TestCase):
    def report(self, obs=None, profiles=None, **kwargs):
        return n.build_report(obs or good_observations(), profiles or ["collect", "test", "render"], "sample", **kwargs)

    def test_all_pass_never_dispatches(self):
        report = self.report()
        self.assertEqual(report["overall"], "preflight_passed")
        self.assertFalse(report["dispatch_eligible"])
        self.assertEqual(report["advertised_capabilities"], [])
        self.assertTrue(all(not item["dispatch_eligible"] for item in report["profiles"]))
        self.assertIn("job_execution", " ".join(report["not_checked"]))

    def test_contract_is_existing_worker_contract(self):
        ref = self.report()["reference"]
        self.assertEqual(ref["contract_version"], "0.4")
        self.assertEqual(ref["distributed_capability"], "web.collect")
        self.assertEqual(ref["collect_modes"], ["search", "extract"])
        self.assertEqual(ref["worker_concurrency"], 1)

    def test_unavailable_unknown_not_checked_remain_distinct(self):
        for state, expected in (("unavailable", "blocked"), ("unknown", "incomplete"), ("not_checked", "incomplete")):
            obs = good_observations()
            obs["package.pydantic"] = n.observation(state, reason="not_requested")
            report = self.report(obs, ["collect"])
            with self.subTest(state=state):
                self.assertEqual(report["overall"], expected)
                self.assertEqual(report["observations"]["package.pydantic"]["status"], state)

    def test_failure_dominates_unknown(self):
        obs = good_observations()
        obs["source_reference_match"] = n.unknown()
        obs["package.pydantic"] = n.observation("unavailable", reason="not_found")
        self.assertEqual(self.report(obs)["overall"], "blocked")

    def test_supported_python_range(self):
        for version, expected in (([3, 11, 9], "blocked"), ([3, 12, 0], "preflight_passed"), ([3, 14, 2], "preflight_passed"), ([3, 15, 0], "blocked"), ([4, 0, 0], "blocked")):
            obs = good_observations()
            obs["python_version"]["value"] = version
            with self.subTest(version=version):
                self.assertEqual(self.report(obs)["overall"], expected)

    def test_dependency_bounds(self):
        for version, expected in (("2.10.99", "blocked"), ("2.11", "preflight_passed"), ("2.11.0", "preflight_passed"), ("2.99.99", "preflight_passed"), ("3.0", "blocked")):
            obs = good_observations()
            obs["package.pydantic"]["value"] = version
            with self.subTest(version=version):
                self.assertEqual(self.report(obs, ["collect"])["overall"], expected)

    def test_collect_does_not_require_git_or_dev_or_gpu(self):
        obs = good_observations()
        for key in ["tool.git", "tool.node", "tool.npm", "tool.pdftoppm", "tool.pdftotext"]:
            obs[key]["value"] = False
        for package in n.DEV_REQUIREMENTS:
            obs[f"package.{package}"] = n.observation("unavailable", reason="not_found")
        self.assertEqual(self.report(obs, ["collect"])["overall"], "preflight_passed")
        self.assertEqual(self.report(obs, ["test"])["overall"], "blocked")
        self.assertEqual(self.report(obs, ["render"])["overall"], "blocked")

    def test_render_is_local_and_does_not_require_worker(self):
        obs = good_observations()
        obs["source_reference_match"] = n.observation("not_checked", reason="not_requested")
        for package in n.RUNTIME_REQUIREMENTS:
            obs[f"package.{package}"] = n.observation("unavailable", reason="not_found")
        report = self.report(obs, ["render"])
        self.assertEqual(report["overall"], "preflight_passed")
        self.assertFalse(report["dispatch_eligible"])

    def test_resource_thresholds_and_unknown_memory(self):
        obs = good_observations()
        self.assertEqual(self.report(obs, min_free_bytes=30*n.GIB)["overall"], "blocked")
        self.assertEqual(self.report(obs, min_available_bytes=9*n.GIB)["overall"], "blocked")
        obs["memory_available_bytes"] = n.unknown("field_not_reported")
        self.assertEqual(self.report(obs)["overall"], "preflight_passed")
        self.assertEqual(self.report(obs, min_available_bytes=1)["overall"], "incomplete")

    def test_source_drift_is_blocked_not_compatible(self):
        obs = good_observations()
        obs["source_reference_match"]["value"] = False
        report = self.report(obs, ["collect"])
        self.assertEqual(report["overall"], "blocked")
        self.assertIn("reference_source_changed", json.dumps(report))

    def test_unsupported_architecture_does_not_claim_package_execution(self):
        obs = good_observations()
        obs["architecture"]["value"] = "other"
        report = self.report(obs)
        self.assertFalse(report["dispatch_eligible"])
        self.assertIn("dependency_imports_extras_transitives_and_lockfile", report["not_checked"])

    def test_bad_options_and_thresholds(self):
        for kwargs in ({"profiles": ["gpu"]}, {"mode": "invented"}, {"profiles": []}, {"min_free_bytes": True}, {"min_available_bytes": -1}):
            options = {"profiles": ["collect"], "mode": "sample", **kwargs}
            with self.subTest(kwargs=kwargs), self.assertRaises(n.InputError):
                n.build_report(good_observations(), **options)

    def test_deterministic_and_does_not_mutate(self):
        obs = good_observations()
        before = copy.deepcopy(obs)
        a = json.dumps(self.report(obs), sort_keys=True)
        b = json.dumps(self.report(obs), sort_keys=True)
        self.assertEqual(a, b)
        self.assertEqual(obs, before)


class ProbeTests(unittest.TestCase):
    def test_linux_memory(self):
        content = b"MemTotal:       1000 kB\nMemAvailable:    300 kB\nMemFree: 1 kB\n"
        with patch("builtins.open", mock_open(read_data=content)) as read:
            total, available = n.memory_linux()
        self.assertEqual(total["value"], 1024000)
        self.assertEqual(available["value"], 307200)
        read.assert_called_once_with("/proc/meminfo", "rb")

    def test_linux_does_not_substitute_free_for_available(self):
        with patch("builtins.open", mock_open(read_data=b"MemTotal: 1000 kB\nMemFree: 900 kB\n")):
            self.assertEqual(n.memory_linux()[1]["status"], "unknown")

    def test_linux_memory_failure_and_inconsistent_measurement(self):
        with patch("builtins.open", side_effect=PermissionError("secret")):
            self.assertEqual(n.memory_linux()[0]["status"], "unknown")
        with patch("builtins.open", mock_open(read_data=b"MemTotal: 10 kB\nMemAvailable: 20 kB\n")):
            self.assertEqual(n.memory_linux()[1]["status"], "unknown")

    def test_windows_memory_mock(self):
        def call(pointer):
            pointer._obj.total_physical = 16*n.GIB
            pointer._obj.available_physical = 6*n.GIB
            self.assertEqual(pointer._obj.length, 64)
            return 1
        fake = Mock()
        fake.GlobalMemoryStatusEx.side_effect = call
        with patch.object(ctypes, "WinDLL", return_value=fake, create=True):
            total, available = n.memory_windows()
        self.assertEqual(total["value"], 16*n.GIB)
        self.assertEqual(available["value"], 6*n.GIB)

    def test_windows_memory_api_failure_mock(self):
        fake = Mock()
        fake.GlobalMemoryStatusEx.return_value = 0
        with patch.object(ctypes, "WinDLL", return_value=fake, create=True):
            self.assertEqual(n.memory_windows()[0]["status"], "unknown")

    def test_macos_memory_mock(self):
        def call(name, value, size, new_value, new_size):
            self.assertEqual(name, b"hw.memsize")
            value._obj.value = 24*n.GIB
            size._obj.value = 8
            return 0
        fake = Mock()
        fake.sysctlbyname.side_effect = call
        with patch.object(ctypes, "CDLL", return_value=fake) as load:
            total, available = n.memory_macos()
        load.assert_called_once_with("/usr/lib/libSystem.B.dylib")
        self.assertEqual(total["value"], 24*n.GIB)
        self.assertEqual(available["status"], "unknown")

    def test_other_os_memory_unknown(self):
        self.assertEqual(n.memory_for_os("Other")[0]["reason"], "unsupported_platform")

    def test_architecture_aliases(self):
        for raw, expected in (("AMD64", "x86_64"), ("aarch64", "arm64"), ("i686", "x86"), ("armv7l", "arm"), ("", "unknown")):
            self.assertEqual(n.normalize_architecture(raw), expected)

    def test_metadata_checks_do_not_import_packages(self):
        with patch.object(n.importlib.metadata, "version", return_value="2.11.3"):
            self.assertEqual(n.package_probe("pydantic")["value"], "2.11.3")
        with patch.object(n.importlib.metadata, "version", side_effect=n.importlib.metadata.PackageNotFoundError):
            self.assertEqual(n.package_probe("pydantic")["status"], "unavailable")
        with patch.object(n.importlib.metadata, "version", return_value="2.11rc1+secretpath"):
            self.assertEqual(n.package_probe("pydantic")["value"], None)

    def test_workspace_probe_does_not_create_or_recurse(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            before = list(root.iterdir())
            result = n.workspace_probe(root)
            self.assertTrue(result["workspace_directory"]["value"])
            self.assertEqual(list(root.iterdir()), before)
            missing = root / "missing"
            self.assertEqual(n.workspace_probe(missing)["workspace_directory"]["status"], "unavailable")
            self.assertFalse(missing.exists())
            file = root / "file"
            file.write_text("x")
            self.assertFalse(n.workspace_probe(file)["workspace_directory"]["value"])

    @unittest.skipUnless(os.name == "posix" and os.open in os.supports_dir_fd, "POSIX source traversal only; Windows fails closed")
    def test_source_probe_only_allowlisted_files_and_crlf(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            reference = {}
            for relative in n.REFERENCE_HASHES:
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"line1\r\nline2\r\n")
                reference[relative] = hashlib.sha256(b"line1\nline2\n").hexdigest()
            with patch.object(n, "REFERENCE_HASHES", reference):
                self.assertTrue(n.source_reference_probe(root)["value"])
                (root / "pyproject.toml").write_bytes(b"changed\n")
                self.assertFalse(n.source_reference_probe(root)["value"])

    @unittest.skipUnless(os.name == "posix" and os.open in os.supports_dir_fd, "POSIX source traversal only; Windows fails closed")
    def test_source_missing_and_symlink(self):
        self.assertEqual(n.source_reference_probe(None)["status"], "not_checked")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(n.source_reference_probe(root)["status"], "unavailable")
            target = root / "target"
            target.mkdir()
            try:
                (root / "src").symlink_to(target, target_is_directory=True)
            except (OSError, NotImplementedError):
                return
            self.assertEqual(n.source_reference_probe(root)["reason"], "unsafe_source_path")

    @unittest.skipUnless(os.name == "posix" and os.open in os.supports_dir_fd, "POSIX source traversal only; Windows fails closed")
    def test_source_read_is_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "src/ephy_worker/contracts.py"
            path.parent.mkdir(parents=True)
            path.write_bytes(b"a" * (n.MAX_SOURCE_BYTES+1))
            self.assertEqual(n.source_reference_probe(root)["reason"], "source_too_large")

    def assert_runtime_static_boundaries(self, source):
        tree = ast.parse(source)
        imports = set()
        aliases = {}
        allowed_imports = {
            "__future__", "argparse", "ctypes", "hashlib", "importlib.metadata",
            "json", "os", "platform", "re", "shutil", "stat", "sys",
            "decimal", "pathlib", "typing",
        }
        for item in ast.walk(tree):
            if isinstance(item, ast.Import):
                imports.update(alias.name for alias in item.names)
                for alias in item.names:
                    aliases[alias.asname or alias.name.split('.')[0]] = (
                        alias.name if alias.asname else alias.name.split('.')[0]
                    )
            elif isinstance(item, ast.ImportFrom) and item.module is not None:
                self.assertEqual(item.level, 0)
                imports.add(item.module)
                for alias in item.names:
                    self.assertNotEqual(alias.name, "*")
                    aliases[alias.asname or alias.name] = item.module + "." + alias.name
            elif isinstance(item, ast.ImportFrom):
                self.fail("relative imports require separate review")
        self.assertFalse(imports - allowed_imports)

        def qualified(node):
            if isinstance(node, ast.Name):
                return aliases.get(node.id, node.id)
            if isinstance(node, ast.Attribute):
                return qualified(node.value) + "." + node.attr
            return ""

        # Freeze the reviewed call surface as well as imports. New aliases or
        # APIs require an explicit checker/review change instead of passing by
        # omission from a blacklist.
        allowed_calls = set("""
            .decode .get .hexdigest .join .splitlines .open
            InputError MemoryStatus OBSERVATION_TYPES.items REFERENCE_HASHES.items
            RedactingParser VERSION_RE.fullmatch _read_fd _read_source_at
            _read_windows_regular all any argparse.ArgumentTypeError build_report
            call check checks.append chunks.append cli.error cli.parse_args close
            collect_observations create ctypes.CDLL ctypes.POINTER ctypes.WinDLL
            ctypes.byref ctypes.c_size_t ctypes.c_uint32 ctypes.c_uint64
            ctypes.c_void_p ctypes.create_string_buffer ctypes.get_last_error
            ctypes.sizeof data.decode data.replace decimal.Decimal dict dict.fromkeys
            get_type gib handle.read hasattr hashlib.sha256 importlib.metadata.version
            info_call inputs.add_argument int isinstance json.dumps json.loads
            kind.endswith len lines.append lines.extend list load_sample main map
            memory_for_os min normalize_architecture obs.update observation observed
            open os.access os.close os.cpu_count os.dup os.fstat os.open os.read
            os.sched_getaffinity package_probe padded_version parser pathlib.Path
            platform.machine platform.system positive_measurement predicate print
            probe profile_result python_version_probe re.compile re.fullmatch read
            read_bounded_regular required.items required.update root.add_argument
            root.add_mutually_exclusive_group self.exit set shutil.disk_usage
            shutil.which sorted source_reference_probe stat.S_ISDIR stat.S_ISREG
            str stream.reconfigure summary sys.exit tuple type unknown valid_value
            validate_observations value value.is_finite value.lower value.startswith
            values.get version.split workspace.stat workspace_probe
        """.split())
        native_bindings = {
            "call": {"kernel.GlobalMemoryStatusEx", "library.sysctlbyname"},
            "create": {"library.CreateFileW"}, "close": {"library.CloseHandle"},
            "get_type": {"library.GetFileType"},
            "info_call": {"library.GetFileInformationByHandleEx"},
            "read": {"library.ReadFile"},
        }
        assignments = {}
        reviewed_stores = set()
        expected_probe = ast.dump(ast.parse(
            '{"Linux": memory_linux, "Windows": memory_windows, "Darwin": memory_macos}.get(system)',
            mode="eval",
        ).body, include_attributes=False)
        for item in ast.walk(tree):
            if isinstance(item, ast.Assign):
                for target in item.targets:
                    if isinstance(target, ast.Name):
                        assignments.setdefault(target.id, []).append(item.value)
                        if target.id in native_bindings:
                            self.assertIn(qualified(item.value), native_bindings[target.id])
                            reviewed_stores.add(id(target))
                        elif target.id == "probe":
                            self.assertEqual(ast.dump(item.value, include_attributes=False), expected_probe)
                            reviewed_stores.add(id(target))

        builtin_calls = {
            "all", "any", "dict", "hasattr", "int", "isinstance", "len", "list",
            "map", "min", "open", "print", "set", "sorted", "str", "tuple", "type",
        }
        for item in ast.walk(tree):
            if isinstance(item, (ast.Import, ast.ImportFrom)):
                for alias in item.names:
                    self.assertNotIn(alias.asname or alias.name.split('.')[0], builtin_calls)

        scope_types = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)

        def local_nodes(scope):
            for child in ast.iter_child_nodes(scope):
                yield child
                if not isinstance(child, scope_types):
                    yield from local_nodes(child)

        # Names are resolved before any call is accepted. Rebinding a builtin,
        # imported namespace, or local call target must not inherit its allowlist.
        for scope in [tree, *(node for node in ast.walk(tree) if isinstance(node, scope_types))]:
            nodes = list(local_nodes(scope))
            called_names = {
                node.func.id for node in nodes
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            }
            protected = builtin_calls | set(aliases) | called_names
            defined = set()
            for node in nodes:
                if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
                    if node.id in protected:
                        self.assertIn(id(node), reviewed_stores)
                elif isinstance(node, ast.arg) and node.arg in protected:
                    self.assertEqual((getattr(scope, "name", None), node.arg), ("check", "predicate"))
                elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    self.assertNotIn(node.name, builtin_calls | set(aliases) | set(native_bindings) | defined)
                    defined.add(node.name)
                elif isinstance(node, ast.ExceptHandler) and node.name is not None:
                    self.assertNotIn(node.name, protected)
                elif isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name is not None:
                    self.assertNotIn(node.name, protected)
                elif isinstance(node, ast.MatchMapping) and node.rest is not None:
                    self.assertNotIn(node.rest, protected)
                elif isinstance(node, ast.Attribute) and isinstance(node.ctx, (ast.Store, ast.Del)):
                    name = qualified(node)
                    self.assertNotIn(name, allowed_calls)
                    self.assertFalse(name.split('.')[0] in builtin_calls | set(aliases))
                    self.assertFalse(any(name in names for names in native_bindings.values()))
                elif isinstance(node, ast.Subscript) and isinstance(node.ctx, (ast.Store, ast.Del)):
                    self.assertNotIn(qualified(node.value).split('.')[0], protected)

        def read_only_flags(node, seen=frozenset()):
            if qualified(node) in {
                "os.O_RDONLY", "os.O_DIRECTORY", "os.O_NOFOLLOW", "os.O_NONBLOCK", "os.O_BINARY",
            }:
                return True
            if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
                return read_only_flags(node.left, seen) and read_only_flags(node.right, seen)
            if isinstance(node, ast.Name):
                if node.id in seen:
                    return False
                values = assignments.get(node.id, [])
                return bool(values) and all(
                    read_only_flags(value, seen | {node.id}) for value in values
                )
            return False

        forbidden = {
            "eval", "exec", "__import__", "system", "popen", "fork", "forkpty",
            "posix_spawn", "posix_spawnp", "startfile", "write", "writelines",
            "write_text", "write_bytes", "touch", "remove", "removedirs", "rmdir",
            "mkdir", "makedirs", "unlink", "rmtree", "rename", "renames", "replace",
            "copy", "copy2", "copyfile", "copytree", "move", "chmod", "fchmod",
            "chown", "fchown", "lchown", "utime", "truncate", "ftruncate",
            "link", "symlink", "mkfifo", "mknod", "putenv", "unsetenv",
        }
        for item in ast.walk(tree):
            if isinstance(item, ast.Name):
                self.assertNotIn(item.id, {"__builtins__", "__loader__", "__spec__"})
            if isinstance(item, ast.Attribute):
                self.assertFalse(item.attr.startswith("__"))
                if item.attr in forbidden:
                    self.assertIn(qualified(item), {"platform.system", "data.replace"})
            if isinstance(item, ast.Call):
                name = qualified(item.func)
                member = name.rsplit(".", 1)[-1]
                if not name:
                    # The sole reviewed computed callable builds FILE_ATTRIBUTE_TAG_INFO.
                    self.assertIsInstance(item.func, ast.BinOp)
                    self.assertIsInstance(item.func.op, ast.Mult)
                    self.assertEqual(qualified(item.func.left), "ctypes.c_uint32")
                    self.assertIsInstance(item.func.right, ast.Constant)
                    self.assertEqual(item.func.right.value, 2)
                else:
                    self.assertIn(name, allowed_calls)
                if name in {"ctypes.WinDLL", "ctypes.CDLL"}:
                    self.assertTrue(item.args)
                    self.assertIsInstance(item.args[0], ast.Constant)
                    self.assertEqual(item.args[0].value, "kernel32" if name.endswith("WinDLL") else "/usr/lib/libSystem.B.dylib")
                if name == "create":
                    self.assertEqual(len(item.args), 7)
                    self.assertFalse(item.keywords)
                    for argument, expected in zip(item.args[1:], (0x80000000, 1, None, 3, 0x00200000, None)):
                        self.assertIsInstance(argument, ast.Constant)
                        self.assertEqual(argument.value, expected)
                if name == "check":
                    self.assertGreaterEqual(len(item.args), 3)
                    self.assertTrue(
                        isinstance(item.args[2], ast.Lambda)
                        or isinstance(item.args[2], ast.Name) and item.args[2].id == "bool"
                    )
                if member in {"open", "fdopen"}:
                    self.assertFalse(any(isinstance(arg, ast.Starred) for arg in item.args))
                    self.assertFalse(any(keyword.arg is None for keyword in item.keywords))
                if name == "os.open":
                    flags = next((k.value for k in item.keywords if k.arg == "flags"), None)
                    flags = flags if flags is not None else item.args[1] if len(item.args) > 1 else None
                    self.assertIsNotNone(flags)
                    self.assertTrue(read_only_flags(flags))
                elif member in {"open", "fdopen"}:
                    offset = 1 if name in {"open", "os.fdopen"} else 0
                    mode = next((k.value for k in item.keywords if k.arg == "mode"), None)
                    mode = mode if mode is not None else item.args[offset] if len(item.args) > offset else ast.Constant("r")
                    self.assertIsInstance(mode, ast.Constant)
                    self.assertIn(mode.value, {"r", "rb", "rt"})
                if member == "system":
                    self.assertEqual(name, "platform.system")
                elif member == "replace":
                    self.assertEqual(name, "data.replace")
                else:
                    self.assertNotIn(member, forbidden)
                    self.assertFalse(member.startswith(("execv", "execl", "spawn")))
                if isinstance(item.func, ast.Name):
                    self.assertNotIn(item.func.id, forbidden)
                if isinstance(item.func, ast.Attribute):
                    # bytes.replace is pure normalization; os.replace is not allowed.
                    if item.func.attr == "system":
                        self.assertIsInstance(item.func.value, ast.Name)
                        self.assertEqual(item.func.value.id, "platform")
                    else:
                        self.assertNotIn(item.func.attr, forbidden - {"replace"})
                    if item.func.attr == "replace":
                        self.assertIsInstance(item.func.value, ast.Name)
                        self.assertEqual(item.func.value.id, "data")

    def test_no_subprocess_network_eval_or_write_calls(self):
        self.assert_runtime_static_boundaries(Path(n.__file__).read_text(encoding="utf-8"))

    def test_static_control_rejects_from_import_modules(self):
        # These strings are parsed only; no network or subprocess code is executed.
        controls = (
            "from urllib import request\nrequest.urlopen('https://example.invalid')",
            "from urllib.request import urlopen as open_url\nopen_url('https://example.invalid')",
            "from subprocess import run\nrun(['never-executed'])",
            "from subprocess import run as launch\nlaunch(['never-executed'])",
            "from socket import socket as connect\nconnect()",
            "from httpx import get\nget('https://example.invalid')",
        )
        for source in controls:
            with self.subTest(source=source), self.assertRaises(AssertionError):
                self.assert_runtime_static_boundaries(source)

    def test_static_control_keeps_direct_import_and_call_rejections(self):
        for source in (
            "import urllib.request as request",
            "import subprocess as child",
            "import socket",
            "import os\nos.system('never-executed')",
            "eval('never-executed')",
            "from pathlib import Path\nPath('never-written').write_text('x')",
        ):
            with self.subTest(source=source), self.assertRaises(AssertionError):
                self.assert_runtime_static_boundaries(source)

    def test_static_control_accepts_harmless_from_import(self):
        self.assert_runtime_static_boundaries(
            "from pathlib import Path\nfrom decimal import Decimal\nimport platform\nplatform.system()"
        )

    def test_static_control_rejects_other_network_imports(self):
        for source in (
            "from http.client import HTTPSConnection",
            "import http.client as client",
            "from ftplib import FTP",
            "import smtplib",
            "import asyncio",
            "from os import *",
        ):
            with self.subTest(source=source), self.assertRaises(AssertionError):
                self.assert_runtime_static_boundaries(source)

    def test_static_control_rejects_file_mutation_apis_and_modes(self):
        controls = (
            "open('output', 'w').write('x')",
            "open('output', mode='a')",
            "open('output', 'r+')",
            "open('output', 'xb')",
            "mode = input()\nopen('output', mode)",
            "options = {'mode': 'w'}\nopen('output', **options)",
            "args = ('output', 'w')\nopen(*args)",
            "from pathlib import Path\nPath('output').open('w')",
            "from pathlib import Path\nPath('output').touch()",
            "import os\nos.remove('output')",
            "import os as operating\noperating.remove('output')",
            "from os import remove as delete\ndelete('output')",
            "writer = open\nwriter('output', 'w')",
            "import os\ndelete = os.remove\ndelete('output')",
            "import os\ngetattr(os, 'remove')('output')",
            "import ctypes\nlibrary = ctypes.WinDLL('kernel32')\nread = library.DeleteFileW\nread('output')",
            "import os\nos.open('output', os.O_WRONLY | os.O_CREAT)",
            "import os\nflags = os.O_RDWR\nos.open('output', flags)",
            "import os\nos.fdopen(3, 'w')",
            "import shutil\nshutil.copyfile('source', 'output')",
        )
        for source in controls:
            with self.subTest(source=source), self.assertRaises(AssertionError):
                self.assert_runtime_static_boundaries(source)

    def test_static_control_accepts_explicit_read_only_opens(self):
        for source in (
            "open('input', 'rb')",
            "open('input', mode='r')",
            "from pathlib import Path\nPath('input').open('rb')",
            "import os\nos.open('input', os.O_RDONLY | os.O_NOFOLLOW)",
        ):
            with self.subTest(source=source):
                self.assert_runtime_static_boundaries(source)

    def test_static_control_rejects_allowlisted_callable_rebinding(self):
        for source in (
            "import os\nopen = os.remove\nopen('output')",
            "open = len\nopen('output')",
            "open: object = len\nopen('output')",
            "(open := len)('output')",
            "for open in (): open('output')",
            "def shadow(open): return open('output')",
            "def open(path): return 1\nopen('output')",
            "from pathlib import Path\nPath = str\nPath('output')",
            "import platform\nimport os\nplatform.system = os.cpu_count\nplatform.system()",
            "import json\njson.loads = json.dumps\njson.loads('{}')",
            "import json\njson.__dict__['loads'] = json.dumps\njson.loads('{}')",
            "__builtins__['open'] = len\nopen('output')",
            "try:\n pass\nexcept Exception as open:\n open('output')",
            "match len:\n case open: open('output')",
        ):
            with self.subTest(source=source), self.assertRaises(AssertionError):
                self.assert_runtime_static_boundaries(source)


    def test_metadata_missing_version_is_unknown(self):
        for value in (None, 42, {}, ["2.11"]):
            with self.subTest(value=value), patch.object(n.importlib.metadata, "version", return_value=value):
                self.assertEqual(n.package_probe("pydantic")["status"], "unknown")
        with patch.object(n.importlib.metadata, "version", side_effect=KeyError("Version")):
            self.assertEqual(n.package_probe("pydantic")["status"], "unknown")

    def test_prerelease_python_cannot_pass_any_profile(self):
        version_info = namedtuple("VersionInfo", "major minor micro releaselevel serial")
        for minor in (12, 13, 14):
            for release in ("alpha", "beta", "candidate"):
                with self.subTest(minor=minor, release=release), patch.object(
                    n.sys, "version_info", version_info(3, minor, 0, release, 1)
                ):
                    item = n.python_version_probe()
                    self.assertEqual(item, n.unknown("non_release_version"))
                    obs = good_observations()
                    obs["python_version"] = item
                    report = n.build_report(obs, ["collect", "test", "render"], "live")
                    self.assertEqual(report["overall"], "incomplete")
                    self.assertTrue(all(p["status"] == "incomplete" for p in report["profiles"]))

    def test_final_python_preserves_numeric_version(self):
        version_info = namedtuple("VersionInfo", "major minor micro releaselevel serial")
        for minor in (12, 13, 14):
            with self.subTest(minor=minor), patch.object(
                n.sys, "version_info", version_info(3, minor, 0, "final", 0)
            ):
                self.assertEqual(n.python_version_probe(), n.observed([3, minor, 0]))

    def test_windows_source_fails_closed_without_reading(self):
        path = Path("unused")
        with patch.object(n.os, "name", "nt"), patch.object(n.os, "open", side_effect=AssertionError("must not inspect reparse paths")):
            self.assertEqual(n.source_reference_probe(path)["reason"], "unsupported_platform")

    @unittest.skipUnless(os.name == "posix" and os.open in os.supports_dir_fd, "POSIX source traversal only; Windows fails closed")
    def test_source_descriptor_traversal_flags(self):
        opened = []
        def fake_open(path, flags, *, dir_fd=None):
            opened.append((path, flags, dir_fd))
            return len(opened)+20
        with patch.object(n.os, "dup", return_value=10), patch.object(n.os, "open", side_effect=fake_open), patch.object(n.os, "close"), patch.object(n, "_read_fd", return_value=b"x"):
            self.assertEqual(n._read_source_at(1, "src/ephy_worker/contracts.py"), b"x")
        self.assertEqual([item[0] for item in opened], ["src", "ephy_worker", "contracts.py"])
        self.assertEqual([item[2] for item in opened], [10, 21, 22])
        self.assertTrue(all(flags & os.O_NOFOLLOW and flags & os.O_NONBLOCK for _, flags, _ in opened))
        self.assertTrue(opened[0][1] & os.O_DIRECTORY)

    def test_source_symlink_swap_cannot_escape(self):
        if os.name != "posix":
            return  # Descriptor-relative path is intentionally unsupported on Windows.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "src/ephy_worker").mkdir(parents=True)
            outside = root / "outside"
            outside.mkdir()
            (outside / "contracts.py").write_text("never read")
            root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
            # Replace an ancestor immediately before traversal. O_NOFOLLOW refuses.
            (root / "src/ephy_worker").rmdir()
            (root / "src/ephy_worker").symlink_to(outside, target_is_directory=True)
            try:
                with self.assertRaises(OSError), patch.object(n, "_read_fd", side_effect=AssertionError("escaped read")):
                    n._read_source_at(root_fd, "src/ephy_worker/contracts.py")
            finally:
                os.close(root_fd)

    def test_windows_sample_reader_rejects_reparse_mock(self):
        fake = Mock()
        fake.CreateFileW.return_value = 123
        fake.GetFileType.return_value = 1
        def fill(handle, category, info, size):
            info._obj[0] = 0x400
            return 1
        fake.GetFileInformationByHandleEx.side_effect = fill
        with patch.object(ctypes, "WinDLL", return_value=fake, create=True):
            with self.assertRaisesRegex(n.InputError, "input_not_regular_file"):
                n._read_windows_regular(Path("sample.json"), 100)
        fake.ReadFile.assert_not_called()
        fake.CloseHandle.assert_called_once_with(123)

    def test_windows_sample_reader_success_mock(self):
        fake = Mock()
        fake.CreateFileW.return_value = 123
        fake.GetFileType.return_value = 1
        def fill(handle, category, info, size):
            info._obj[0] = 0x80
            return 1
        def read(handle, buffer, capacity, count, overlapped):
            buffer.value = b"abc"
            count._obj.value = 3
            return 1
        fake.GetFileInformationByHandleEx.side_effect = fill
        fake.ReadFile.side_effect = read
        with patch.object(ctypes, "WinDLL", return_value=fake, create=True):
            self.assertEqual(n._read_windows_regular(Path("sample.json"), 100), b"abc")
        fake.CloseHandle.assert_called_once_with(123)


class CliTests(unittest.TestCase):
    def invoke(self, args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = n.main(args)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_sample_cli_never_probes_host(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/"sample.json"
            path.write_text(sample_json())
            with patch.object(n, "collect_observations", side_effect=AssertionError("host probe forbidden")), patch.object(n, "memory_for_os", side_effect=AssertionError("host probe forbidden")), patch.object(n.shutil, "which", side_effect=AssertionError("host probe forbidden")):
                code, output, error = self.invoke(["--sample", str(path), "--format", "both"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)["mode"], "sample")
        self.assertIn("SAMPLE", error)
        self.assertNotIn(directory, output+error)

    def test_cli_exit_codes(self):
        for state, code in (("observed", 0), ("unavailable", 1), ("unknown", 3), ("not_checked", 3)):
            obs = good_observations()
            if state != "observed":
                obs["package.pydantic"] = n.observation(state, reason="not_requested")
            with patch.object(n, "load_sample", return_value=obs):
                with self.subTest(state=state):
                    self.assertEqual(self.invoke(["--sample", "not-read"])[0], code)

    def test_input_error_is_redacted(self):
        code, output, error = self.invoke(["--sample", "/sensitive-personal-path/not-found.json"])
        self.assertEqual(code, 2)
        self.assertEqual(output, "")
        self.assertNotIn("sensitive", error)
        self.assertEqual(json.loads(error)["error"], "input_not_found")

    def test_no_secrets_in_report(self):
        report = n.build_report(good_observations(), ["collect"], "sample")
        content = json.dumps(report)
        self.assertNotIn("hostname", content)
        self.assertNotIn("executable_path", content)
        self.assertNotIn("environment_variables", content)
        self.assertTrue(all(value is False for value in report["privacy"].values()))

    def test_gib_bounds(self):
        self.assertEqual(n.gib_argument("1.5"), 3*n.GIB//2)
        for raw in ("NaN", "Infinity", "-1", "1048577", "garbage"):
            with self.subTest(raw=raw), self.assertRaises(n.argparse.ArgumentTypeError):
                n.gib_argument(raw)


    def test_argument_errors_do_not_echo_private_tokens(self):
        for args in (["--workspace", ".", "--profile", "secret-person-name"], ["--workspace", ".", "--unknown-secret-path"]):
            stderr = io.StringIO()
            with redirect_stderr(stderr), self.assertRaises(SystemExit):
                n.main(args)
            self.assertEqual(stderr.getvalue(), '{"error":"invalid_arguments"}\n')

    def test_incompatible_input_flags(self):
        with self.assertRaises(SystemExit) as exit_result:
            self.invoke(["--sample", "x", "--worker-source", "y"])
        self.assertEqual(exit_result.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
