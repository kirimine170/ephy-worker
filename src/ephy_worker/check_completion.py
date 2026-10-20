"""Pinned post-return evidence for checker/pytest commands; no bare exit0 success．"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import runpy
import secrets
import stat
import sys
import traceback
from pathlib import Path


def _bytes(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")


def _hash(raw):
    return hashlib.sha256(raw).hexdigest()


def _read(path):
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise ValueError("completion_record_invalid")
            value[key] = item
        return value
    return json.loads(path.read_bytes(), object_pairs_hook=pairs)


def _entry(argv):
    index = 1
    while index < len(argv):
        value = argv[index]
        if value in {"-I", "-B", "-E", "-s", "-S", "-u", "-P"}:
            index += 1
        elif value in {"-X", "-W"}:
            index += 2
        elif value.startswith(("-X", "-W")):
            index += 1
        else:
            break
    return index


def prepare_completion(argv, *, checker, helper, request_dir, output_dir, label, cwd, trusted_scripts=None):
    """Retain requested argv separately from its explicit completion transport．"""
    index = _entry(argv)
    if index >= len(argv):
        return None
    script, script_hash, exit_line, nested_exit = None, None, None, None
    if argv[index:index + 2] == ["-m", "pytest"]:
        mode, arguments = "pytest", argv[index + 2:]
    elif Path(argv[index]).resolve() == Path(checker).resolve():
        mode, script, arguments = "checker", str(Path(checker).resolve()), argv[index + 1:]
    elif trusted_scripts and str(Path(argv[index]).resolve()) in trusted_scripts:
        script = str(Path(argv[index]).resolve())
        raw = Path(script).read_bytes()
        script_hash = _hash(raw)
        if script_hash != trusted_scripts[script]:
            raise ValueError("completion_trusted_script_changed")
        # The fixed registered wrapper owns one literal CLI exit boundary．
        tree = ast.parse(raw)
        sites = [node for node in ast.walk(tree) if isinstance(node, ast.Raise)
            and isinstance(node.exc, ast.Call) and isinstance(node.exc.func, ast.Name)
            and node.exc.func.id == "SystemExit" and len(node.exc.args) == 1
            and isinstance(node.exc.args[0], ast.Call) and isinstance(node.exc.args[0].func, ast.Name)
            and node.exc.args[0].func.id == "main" and not node.exc.args[0].args and not node.exc.args[0].keywords]
        if len(sites) != 1 or Path(script).name not in {"registered_checks.py", "registered_checks_v3.py"}:
            raise ValueError("completion_trusted_entrypoint_invalid")
        mode, arguments, exit_line = "registered", argv[index + 1:], sites[0].lineno
        if len(arguments) == 3 and arguments[0] == "repository":
            validator = Path(arguments[1]).resolve() / "scripts" / "validate_repository.py"
            validation_raw = validator.read_bytes()
            validation_sites = [node for node in ast.walk(ast.parse(validation_raw)) if isinstance(node, ast.Raise)
                and isinstance(node.exc, ast.Call) and isinstance(node.exc.func, ast.Name)
                and node.exc.func.id == "SystemExit" and len(node.exc.args) == 1
                and isinstance(node.exc.args[0], ast.Call) and isinstance(node.exc.args[0].func, ast.Name)
                and node.exc.args[0].func.id == "main" and not node.exc.args[0].args and not node.exc.args[0].keywords]
            if len(validation_sites) != 1:
                raise ValueError("completion_trusted_entrypoint_invalid")
            nested_exit = {"path": str(validator), "sha256": _hash(validation_raw), "line": validation_sites[0].lineno}
    else:
        return None
    request_dir, output_dir = Path(request_dir), Path(output_dir)
    request_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    helper_raw = Path(helper).read_bytes()
    helper_hash = _hash(helper_raw)
    staged = request_dir.parent / ("check-completion-" + helper_hash + ".py")
    if not staged.exists():
        with staged.open("xb") as stream:
            stream.write(helper_raw)
    info = staged.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or staged.read_bytes() != helper_raw
            or Path(helper).read_bytes() != helper_raw):
        raise ValueError("completion_helper_changed")
    nonce = secrets.token_hex(24)
    request_path, record_path = request_dir / (nonce + ".json"), output_dir / (nonce + ".completion.json")
    request = {"schema": "ephy.check-completion-request.v1", "nonce": nonce, "label": label,
        "argv": list(argv), "cwd": str(Path(cwd).resolve()), "mode": mode, "script": script,
        "script_sha256": script_hash, "exit_line": exit_line, "nested_exit": nested_exit, "helper_sha256": helper_hash,
        "arguments": arguments, "record": str(record_path)}
    raw = _bytes(request)
    with request_path.open("xb") as stream:
        stream.write(raw)
    transport = [*argv[:index], str(staged), "--request", str(request_path), "--request-sha256", _hash(raw)]
    return {"requested_argv": list(argv), "argv": transport, "request": str(request_path),
        "request_sha256": _hash(raw), "record": str(record_path), "helper": str(staged), "helper_sha256": helper_hash}


def verify_completion(plan, exit_code):
    path = Path(plan["record"])
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 16384:
            raise ValueError("completion_record_invalid")
        request_raw = Path(plan["request"]).read_bytes()
        if _hash(request_raw) != plan["request_sha256"]:
            raise ValueError("completion_request_changed")
        if _hash(Path(plan["helper"]).read_bytes()) != plan["helper_sha256"]:
            raise ValueError("completion_helper_changed")
        request, record = _read(Path(plan["request"])), _read(path)
        expected = {"schema": "ephy.check-completion-record.v1", "nonce": request["nonce"],
            "label": request["label"], "request_sha256": plan["request_sha256"],
            "argv_sha256": _hash(_bytes(plan["requested_argv"])), "cwd": request["cwd"], "exit_code": exit_code}
        if (not isinstance(record, dict) or set(record) != set(expected) | {"reported_pid", "outcome"}
                or any(record[key] != value for key, value in expected.items())
                or type(record["reported_pid"]) is not int or record["reported_pid"] <= 0
                or record["outcome"] not in {"returned", "exception"}
                or record["outcome"] == "exception" and exit_code != 1):
            raise ValueError("completion_record_invalid")
        return {"path": str(path), "sha256": _hash(path.read_bytes()), "record": record}
    except FileNotFoundError:
        raise ValueError("completion_record_missing") from None


def _deny_nested(event, _arguments):
    if event in {"subprocess.Popen", "os.system", "os.fork", "os.forkpty", "os.posix_spawn",
                 "os.spawn", "os.exec", "os.startfile", "os.startfile/2", "ctypes.dlopen"}:
        raise RuntimeError("unsupported_nested_checker_command")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", required=True, type=Path)
    parser.add_argument("--request-sha256", required=True)
    args = parser.parse_args()
    raw = args.request.read_bytes()
    if _hash(raw) != args.request_sha256:
        raise ValueError("completion_request_changed")
    request = _read(args.request)
    if _hash(Path(__file__).read_bytes()) != request["helper_sha256"]:
        raise ValueError("completion_helper_changed")
    if str(Path.cwd().resolve()) != request["cwd"]:
        raise ValueError("completion_cwd_changed")
    outcome, exit_code = "returned", 0
    try:
        if request["mode"] == "pytest":
            import pytest
            exit_code = int(pytest.main(request["arguments"]))
        else:
            if request["mode"] == "registered":
                if _hash(Path(request["script"]).read_bytes()) != request["script_sha256"]:
                    raise ValueError("completion_trusted_script_changed")
            elif request["mode"] == "checker":
                sys.addaudithook(_deny_nested)
            else:
                raise ValueError("completion_mode_invalid")
            sys.argv = [request["script"], *request["arguments"]]
            try:
                runpy.run_path(request["script"], run_name="__main__")
            except SystemExit as exc:
                frame = exc.__traceback__
                while frame.tb_next is not None:
                    frame = frame.tb_next
                sites = [{"path": request["script"], "line": request["exit_line"], "sha256": request["script_sha256"]}]
                if request["nested_exit"] is not None:
                    sites.append(request["nested_exit"])
                if (request["mode"] != "registered" or type(exc.code) is not int or not any(
                        frame.tb_frame.f_code.co_filename == site["path"] and frame.tb_lineno == site["line"]
                        and _hash(Path(site["path"]).read_bytes()) == site["sha256"] for site in sites)):
                    raise
                exit_code = exc.code
    except Exception:  # noqa: BLE001 — completed exceptions remain failed checks with retained traceback．
        traceback.print_exc()
        outcome, exit_code = "exception", 1
    # os._exit and unexpected SystemExit never reach this trusted post-return step．
    record = {"schema": "ephy.check-completion-record.v1", "nonce": request["nonce"],
        "label": request["label"], "request_sha256": args.request_sha256,
        "argv_sha256": _hash(_bytes(request["argv"])), "cwd": request["cwd"],
        "exit_code": exit_code, "reported_pid": os.getpid(), "outcome": outcome}
    with Path(request["record"]).open("xb") as stream:
        stream.write(_bytes(record))
        stream.flush()
        os.fsync(stream.fileno())
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
