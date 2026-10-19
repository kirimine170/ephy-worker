"""Pinned native Windows verifier isolation, with measured negative controls.

This module launches commands, never a Codex model. The trusted controller owns
the descriptor and evidence; neither the candidate nor a Pi stage may edit them.
"""

from __future__ import annotations

import json
import os
import subprocess
import uuid
from contextlib import contextmanager
from pathlib import Path

from .formal_artifacts import GateFailure, digest, encode, file_hash, read_json, write_json

PROFILE = "native_windows_code_v1"
NETWORK_POLICIES = {"windows_all_network_denied", "windows_external_network_denied"}
SCHEMA = "ephy.native-windows-verifier.v1"
CONTROL_KEYS = {
    "inside_write", "protected_read_denied", "protected_write_denied",
    "candidate_write_denied", "checker_write_denied", "child_read_denied",
    "child_write_denied", "network_denied", "loopback_denied", "child_network_denied",
    "child_loopback_denied", "child_candidate_write_denied", "child_checker_write_denied", "asyncio_completed",
    "candidate_read", "checker_read", "nested_temp_read_write", "child_candidate_read", "child_checker_read",
    "child_nested_temp_read_write",
    "parent_metadata", "parent_enumeration_denied", "sibling_read_denied", "sibling_write_denied",
    "child_parent_metadata", "child_parent_enumeration_denied", "child_sibling_read_denied",
    "child_sibling_write_denied",
}

# This script has no candidate imports. A child repeats protected operations to
# test inherited restrictions rather than merely trusting the parent process.
PROBE = r'''import asyncio, json, os, pathlib, socket, subprocess, sys
nonce, protected, candidate, checker, temporary, sibling = sys.argv[1:]
def denied(operation):
    try: operation()
    except PermissionError: return True
    return False
protected = pathlib.Path(protected)
result = {"inside_write": False}
pathlib.Path(temporary, "inside-" + nonce).write_text(nonce)
result["inside_write"] = True
result["candidate_read"] = bool(pathlib.Path(candidate).read_bytes())
result["checker_read"] = bool(pathlib.Path(checker).read_bytes())
nested=pathlib.Path(temporary,"nested"); nested.mkdir()
(nested/"control").write_text(nonce)
result["nested_temp_read_write"] = (nested/"control").read_text()==nonce
result["protected_read_denied"] = denied(protected.read_bytes)
def writable_open(path):
    handle=os.open(path,os.O_WRONLY|os.O_APPEND);os.close(handle)
result["protected_write_denied"] = denied(lambda: writable_open(protected))
result["candidate_write_denied"] = denied(lambda: pathlib.Path(candidate).open("r+b").close())
result["checker_write_denied"] = denied(lambda: pathlib.Path(checker).open("r+b").close())
parent=pathlib.Path(candidate).parent.parent
result["parent_metadata"]=parent.is_dir()
result["parent_enumeration_denied"]=denied(lambda:list(parent.iterdir()))
result["sibling_read_denied"]=denied(pathlib.Path(sibling).read_bytes)
result["sibling_write_denied"]=denied(lambda:writable_open(sibling))
child = subprocess.run([sys.executable, "-I", "-c", """import json,os,pathlib,socket,sys
p=pathlib.Path(sys.argv[1]); out={}
def writable_open(path):
 handle=os.open(path,os.O_WRONLY|os.O_APPEND);os.close(handle)
def denied(operation):
 try: operation()
 except PermissionError:return True
 return False
parent=pathlib.Path(sys.argv[2]).parent.parent
out['child_parent_metadata']=parent.is_dir()
out['child_parent_enumeration_denied']=denied(lambda:list(parent.iterdir()))
out['child_sibling_read_denied']=denied(pathlib.Path(sys.argv[5]).read_bytes)
out['child_sibling_write_denied']=denied(lambda:writable_open(sys.argv[5]))
out['child_candidate_read']=bool(pathlib.Path(sys.argv[2]).read_bytes())
out['child_checker_read']=bool(pathlib.Path(sys.argv[3]).read_bytes())
nested=pathlib.Path(sys.argv[4],'child-nested');nested.mkdir()
(nested/'control').write_text('child');out['child_nested_temp_read_write']=(nested/'control').read_text()=='child'
for name, op in [('child_read_denied',p.read_bytes),('child_write_denied',lambda:writable_open(p)),
 ('child_candidate_write_denied',lambda:pathlib.Path(sys.argv[2]).open('r+b').close()),
 ('child_checker_write_denied',lambda:pathlib.Path(sys.argv[3]).open('r+b').close())]:
 try: op(); out[name]=False
 except PermissionError: out[name]=True
for name, host, port in [('child_network_denied','1.1.1.1',443),('child_loopback_denied','127.0.0.1',18080)]:
 s=socket.socket(); s.settimeout(1)
 try: s.connect((host,port)); out[name]=False
 except OSError as exc: out[name]=isinstance(exc,PermissionError) or getattr(exc,'winerror',None)==10013
 finally: s.close()
print(json.dumps(out))
""", str(protected), candidate, checker, temporary, sibling], capture_output=True, text=True, timeout=15)
if child.returncode: raise RuntimeError("control child failed")
result.update(json.loads(child.stdout))
async def observe():
    await asyncio.sleep(0.01)
    return True
result["asyncio_completed"] = asyncio.run(observe())
for name, host, port in [('network_denied','1.1.1.1',443),('loopback_denied','127.0.0.1',18080)]:
    sock=socket.socket(); sock.settimeout(1)
    try: sock.connect((host,port)); result[name]=False
    except OSError as exc:
        # Timeout/unreachable/refused is not proof of enforced network denial.
        result[name] = isinstance(exc,PermissionError) or getattr(exc,'winerror',None)==10013
    finally: sock.close()
print(json.dumps({"nonce":nonce,"controls":result},sort_keys=True))
'''


def _path(value: str | Path, *, exists: bool = True) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise GateFailure("Sandbox paths must be absolute")
    return path.resolve(strict=exists)


def _toml_string(value: str) -> str:
    return json.dumps(value.replace("\\", "/"), ensure_ascii=True)


def profile_override(
    descriptor: dict, *, candidate: Path, checker: Path, temporary: Path,
    extra_reads: tuple[Path, ...] = (),
) -> str:
    """One complete inline profile; no global configuration is modified."""
    private = [_path(path) for path in descriptor["denied_roots"]]
    readonly = [_path(path) for path in (candidate, checker.parent, *extra_reads)]
    temporary = _path(temporary)
    if any(root == denied or denied.is_relative_to(root) for root in readonly for denied in private):
        raise GateFailure("Sandbox readonly allowance reopens a broad private root")
    if any(temporary == root or temporary.is_relative_to(root) or root.is_relative_to(temporary)
           for root in readonly):
        raise GateFailure("Sandbox writable temp overlaps readonly candidate/checker roots")
    roots = {path: "deny" for path in private}
    for path in descriptor["read_roots"]:
        roots[_path(path)] = "read"
    for path in readonly:
        roots[path] = "read"
    roots[temporary] = "write"
    entries = [ '":root"="read"', '":minimal"="read"' ]
    entries.extend(_toml_string(str(path)) + "=" + _toml_string(mode) for path, mode in roots.items())
    return "permissions.ephy_verifier={filesystem={" + ",".join(entries) + "},network={enabled=false}}"


def sandbox_argv(descriptor: dict, candidate: Path, profile: str, argv: list[str]) -> list[str]:
    return [
        descriptor["codex"]["path"], "sandbox", "-C", str(candidate), "-P", "ephy_verifier",
        "--include-managed-config", "-c", profile, "--", *argv,
    ]


ACL_SCRIPT = r'''$ErrorActionPreference='Stop'
$payload=[Console]::In.ReadToEnd() | ConvertFrom-Json
$group=[Security.Principal.NTAccount]::new([Environment]::MachineName,'CodexSandboxUsers')
$sid=$group.Translate([Security.Principal.SecurityIdentifier])
function InheritableAceBytes($text) {
  $raw=[Security.AccessControl.RawSecurityDescriptor]::new($text)
  $values=@()
  foreach($ace in $raw.DiscretionaryAcl) {
    if(([int]$ace.AceFlags) -band 19) {
      $bytes=[byte[]]::new($ace.BinaryLength);$ace.GetBinaryForm($bytes,0)
      $values+=[Convert]::ToBase64String($bytes)
    }
  }
  return ($values -join ',')
}
$records=@()
foreach($item in $payload.items) {
  $path=[IO.Path]::GetFullPath($item.path)
  $acl=Get-Acl -LiteralPath $path
  if($payload.action -eq 'observe') {
    $records+=@{path=$path;sddl=$acl.GetSecurityDescriptorSddlForm([Security.AccessControl.AccessControlSections]::Access)}
  } else {
    $sddl=$acl.GetSecurityDescriptorSddlForm([Security.AccessControl.AccessControlSections]::Access)
    $records+=@{path=$path;sddl=$sddl}
    if($payload.action -eq 'prepare_metadata') {
      $remaining=0x1000a0
      foreach($entry in $acl.Access) {
        if($entry.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value -eq $sid.Value -and
           -not ($entry.PropagationFlags -band [Security.AccessControl.PropagationFlags]::InheritOnly)) {
          $mask=([int]$entry.FileSystemRights) -band $remaining
          if($mask -and $entry.AccessControlType -eq 'Deny') { break }
          if($entry.AccessControlType -eq 'Allow') { $remaining=$remaining -band (-bnot [int]$entry.FileSystemRights) }
          if(-not $remaining) { break }
        }
      }
      if(-not $remaining) { $records[-1].prepared_sddl=$sddl;continue }
    }
    # Keep all non-sandbox ACLs, but stop ancestor sandbox DENY rules from
    # defeating the explicitly declared permission in new child directories.
    # Only this exact narrow root is protected; global private ACLs stay intact.
    if($payload.action -ne 'prepare_metadata') { $acl.SetAccessRuleProtection($true,$true) }
    foreach($rule in @($acl.Access)) {
      if(-not $rule.IsInherited -and $rule.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value -eq $sid.Value) {
        $keep=($payload.action -eq 'prepare_metadata' -and
          $rule.InheritanceFlags -ne [Security.AccessControl.InheritanceFlags]::None)
        if($keep -and $rule.AccessControlType -eq 'Deny' -and
           -not ($rule.PropagationFlags -band [Security.AccessControl.PropagationFlags]::InheritOnly) -and
           ($rule.FileSystemRights -band [Security.AccessControl.FileSystemRights]::ReadAttributes)) {
          throw 'Metadata permission cannot alter an explicit inheritable private DENY'
        }
        if(-not $keep) {
          [void]$acl.RemoveAccessRuleSpecific($rule)
        }
      }
    }
    if($payload.action -eq 'prepare_metadata') {
      $deny=[Security.AccessControl.FileSystemAccessRule]::new($sid,'ReadData,ReadExtendedAttributes,ReadPermissions,WriteData,AppendData,WriteAttributes,WriteExtendedAttributes,DeleteSubdirectoriesAndFiles,Delete,ChangePermissions,TakeOwnership','Deny')
      $acl.AddAccessRule($deny)
      $rule=[Security.AccessControl.FileSystemAccessRule]::new($sid,'ReadAttributes,Traverse,Synchronize','Allow')
    } elseif(Test-Path -LiteralPath $path -PathType Container) {
      $rights=if($payload.action -eq 'prepare_write') {'Modify'} else {'ReadAndExecute'}
      $rule=[Security.AccessControl.FileSystemAccessRule]::new($sid,$rights,'ContainerInherit,ObjectInherit','None','Allow')
    } else { $rule=[Security.AccessControl.FileSystemAccessRule]::new($sid,'ReadAndExecute','Allow') }
    $acl.AddAccessRule($rule)
    if($payload.action -eq 'prepare_metadata') {
      $records[-1].prepared_sddl=$acl.GetSecurityDescriptorSddlForm([Security.AccessControl.AccessControlSections]::Access)
      $old=[Security.AccessControl.RawSecurityDescriptor]::new($sddl)
      $new=[Security.AccessControl.RawSecurityDescriptor]::new($records[-1].prepared_sddl)
      if($old.ControlFlags -ne $new.ControlFlags -or
         (InheritableAceBytes $sddl) -ne (InheritableAceBytes $records[-1].prepared_sddl)) {
        throw 'Metadata permission must preserve inheritance flags and inheritable ACE bytes'
      }
    } else { Set-Acl -LiteralPath $path -AclObject $acl }
  }
}
ConvertTo-Json -InputObject @($records) -Compress -Depth 6
'''


def _acl_operation(descriptor: dict, action: str, items: list[dict]) -> list[dict]:
    import base64

    if action not in {"observe", "prepare", "prepare_write", "prepare_metadata", "restore", "restore_metadata"}:
        raise GateFailure("Unknown scoped native ACL operation")
    if action in {"restore", "restore_metadata"}:
        current = _acl_operation(descriptor, "observe", items)
        failures = []
        for entry, observed in zip(items, current, strict=True):
            if entry != observed:
                try:
                    _restore_dacl(entry, propagate=action != "restore_metadata")
                except GateFailure as exc:
                    failures.append(str(exc))
        observed = _acl_operation(descriptor, "observe", items)
        if observed != items or failures:
            raise GateFailure("Exact native DACL/inheritance restoration mismatch: " + "; ".join(failures))
        return []
    powershell = Path(descriptor["acl_helper"]["path"])
    if file_hash(powershell) != descriptor["acl_helper"]["sha256"]:
        raise GateFailure("Native ACL helper binary changed")
    environment = {key: os.environ[key] for key in (
        "PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "COMPUTERNAME", "USERPROFILE",
    ) if key in os.environ}
    result = subprocess.run(
        [str(powershell), "-NoProfile", "-NonInteractive", "-EncodedCommand",
         base64.b64encode(ACL_SCRIPT.encode("utf-16le")).decode("ascii")],
        input=json.dumps({"action": action, "items": items}).encode("utf-8"), capture_output=True,
        timeout=120, check=False, creationflags=0x08000000, env=environment,
    )
    if result.returncode:
        raise GateFailure("Narrow readonly sandbox ACL " + action + " failed: "
                          + result.stderr.decode("utf-8", errors="replace")[-2000:])
    records = json.loads(result.stdout.decode("utf-8-sig"))
    if action == "prepare_metadata":
        for entry in records:
            if entry["sddl"] != entry["prepared_sddl"]:
                _restore_dacl({"path": entry["path"], "sddl": entry["prepared_sddl"]}, propagate=False)
    return records


def _restore_dacl(entry: dict, *, propagate: bool = True) -> None:
    """Set only the saved DACL and its inheritance flag; never request a SACL.

    PowerShell Set-Acl may request SeSecurityPrivilege when unprotecting an ACL.
    The native API explicitly scopes this operation to DACL_SECURITY_INFORMATION.
    """
    import ctypes
    from ctypes import wintypes as w

    api = ctypes.WinDLL("advapi32", use_last_error=True)
    pointer = ctypes.c_void_p
    api.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        w.LPCWSTR, w.DWORD, ctypes.POINTER(pointer), ctypes.POINTER(w.DWORD),
    ]
    api.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = w.BOOL
    api.GetSecurityDescriptorDacl.argtypes = [
        pointer, ctypes.POINTER(w.BOOL), ctypes.POINTER(pointer), ctypes.POINTER(w.BOOL),
    ]
    api.GetSecurityDescriptorDacl.restype = w.BOOL
    api.GetSecurityDescriptorControl.argtypes = [pointer, ctypes.POINTER(w.WORD), ctypes.POINTER(w.DWORD)]
    api.GetSecurityDescriptorControl.restype = w.BOOL
    api.SetNamedSecurityInfoW.argtypes = [w.LPWSTR, w.DWORD, w.DWORD, pointer, pointer, pointer, pointer]
    api.SetNamedSecurityInfoW.restype = w.DWORD
    api.SetSecurityInfo.argtypes = [w.HANDLE, w.DWORD, w.DWORD, pointer, pointer, pointer, pointer]
    api.SetSecurityInfo.restype = w.DWORD
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, pointer, w.DWORD, w.DWORD, w.HANDLE]
    kernel.CreateFileW.restype = w.HANDLE
    kernel.CloseHandle.argtypes = [w.HANDLE]
    kernel.CloseHandle.restype = w.BOOL
    memory, size = pointer(), w.DWORD()
    if not api.ConvertStringSecurityDescriptorToSecurityDescriptorW(
        entry["sddl"], 1, ctypes.byref(memory), ctypes.byref(size),
    ):
        raise GateFailure("Saved native DACL cannot be decoded")
    try:
        present, defaulted, acl = w.BOOL(), w.BOOL(), pointer()
        if not api.GetSecurityDescriptorDacl(memory, ctypes.byref(present), ctypes.byref(acl),
                                              ctypes.byref(defaulted)) or not present.value or not acl.value:
            raise GateFailure("Saved native DACL must be present and non-null")
        control, revision = w.WORD(), w.DWORD()
        if not api.GetSecurityDescriptorControl(memory, ctypes.byref(control), ctypes.byref(revision)):
            raise GateFailure("Saved native DACL inheritance cannot be observed")
        # SE_DACL_PROTECTED; PROTECTED/UNPROTECTED_DACL_SECURITY_INFORMATION.
        flags = 4 | (0x80000000 if control.value & 0x1000 else 0x20000000)
        if propagate:
            code = api.SetNamedSecurityInfoW(entry["path"], 1, flags, None, None, acl, None)
            if code:
                raise GateFailure("Native DACL-only restoration failed: " + str(code))
        else:
            # Microsoft documents that MAXIMUM_ALLOWED suppresses propagation
            # in SetSecurityInfo. Preserve the original control flags/child ACEs.
            handle = kernel.CreateFileW(entry["path"], 0x02000000, 7, None, 3, 0x02000000, None)
            if handle == ctypes.c_void_p(-1).value:
                raise GateFailure("Cannot open exact metadata ancestor " + entry["path"] + ": "
                                  + str(ctypes.get_last_error()))
            try:
                code = api.SetSecurityInfo(handle, 1, flags, None, None, acl, None)
                if code:
                    raise GateFailure("Native self-only metadata DACL operation failed: " + str(code))
            finally:
                kernel.CloseHandle(handle)
    finally:
        kernel.LocalFree.argtypes = [pointer]
        kernel.LocalFree.restype = pointer
        kernel.LocalFree(memory)


@contextmanager
def _scoped_acl(descriptor: dict, paths: list[Path], evidence: Path, *, writable: bool, metadata: bool = False):
    """Temporarily grant the sandbox account read access on exact narrow roots.

    The CLI may leave a private parent DENY inherited by readonly children. Only
    explicit rules on these operator-selected roots are changed, then restored;
    broad home/workspace rules are never removed. The complete pre-change DACL
    bytes are retained before permitting a child command.
    """
    if os.name != "nt":
        raise GateFailure("Readonly native sandbox ACL preparation requires Windows")
    roots = sorted({_path(path) for path in paths}, key=lambda path: (len(path.parts), str(path)))
    private = [_path(path) for path in descriptor["denied_roots"]]
    if any((not metadata and root == denied) or denied.is_relative_to(root) and root != denied
           for root in roots for denied in private):
        raise GateFailure("Readonly ACL preparation cannot reopen broad private roots")
    if writable and (len(roots) != 1 or not roots[0].is_dir()
                     or not any(roots[0].is_relative_to(path) for path in private)):
        raise GateFailure("Writable ACL preparation requires one exact job temp under a private root")
    if metadata and (writable or not roots or any(not root.is_dir() for root in roots)
                     or any(not any(root.is_relative_to(path) for path in private) for root in roots)):
        raise GateFailure("Metadata ACL preparation requires exact private ancestors of declared roots")
    # A nested read root inherits the outer grant. Avoid redundant ACL edits.
    if not metadata:
        roots = [root for index, root in enumerate(roots)
                 if not any(root.is_relative_to(parent) for parent in roots[:index])]
    records = []
    try:
        records = _acl_operation(descriptor, "observe", [{"path": str(root)} for root in roots])
        if len(records) != len(roots):
            raise GateFailure("Readonly ACL snapshot is incomplete")
        # Snapshot every root before a batch can make its first change. Even
        # partial preparation restores all original DACLs in reverse order.
        write_json(evidence, {"schema": "ephy.native-scoped-acl.v1", "before": records,
                             "permission": "ReadAttributes+Traverse" if metadata else (
                                 "Modify" if writable else "ReadAndExecute")}, exclusive=False)
        action = "prepare_metadata" if metadata else "prepare_write" if writable else "prepare"
        _acl_operation(descriptor, action, [{"path": str(root)} for root in roots])
        yield {"path": str(evidence), "sha256": file_hash(evidence)}
    finally:
        if records:
            _acl_operation(descriptor, "restore_metadata" if metadata else "restore", list(reversed(records)))
            write_json(evidence.with_suffix(".restored.json"), {
                "before_sha256": file_hash(evidence), "restored": True,
            })


def readonly_acl(descriptor: dict, paths: list[Path], evidence: Path):
    return _scoped_acl(descriptor, paths, evidence, writable=False)


def writable_acl(descriptor: dict, temporary: Path, evidence: Path):
    return _scoped_acl(descriptor, [temporary], evidence, writable=True)


def metadata_ancestors(descriptor: dict, roots: list[Path]) -> list[Path]:
    """Exact ancestor chain inside private roots; no directory-data permission."""
    private = [_path(path) for path in descriptor["denied_roots"]]
    ancestors = set()
    for value in roots:
        for parent in _path(value).parents:
            if any(parent.is_relative_to(root) for root in private):
                ancestors.add(parent)
    return sorted(ancestors, key=lambda path: (len(path.parts), str(path)))


def metadata_acl(descriptor: dict, roots: list[Path], evidence: Path):
    return _scoped_acl(descriptor, metadata_ancestors(descriptor, roots), evidence,
                       writable=False, metadata=True)


def run_isolated(
    descriptor: dict, argv: list[str], *, candidate: Path, checker: Path, temp: Path,
    cwd: Path, environment: dict[str, str], timeout: int,
) -> dict:
    """Execute a trusted fixed check argv under the measured descriptor.

    This helper is also used for pre-submission checker controls. It never
    interprets a shell string, forwards secrets, or substitutes a host fallback.
    """
    validate_descriptor(descriptor)
    if os.name != "nt":
        raise GateFailure("Executable verifier profile requires native Windows isolation")
    candidate, checker, temp, cwd = map(_path, (candidate, checker, temp, cwd))
    if cwd != candidate or temp.is_relative_to(candidate) or temp.is_relative_to(checker.parent):
        raise GateFailure("Verifier cwd must be the readonly candidate; temp must be separate")
    if not isinstance(argv, list) or not argv or any(not isinstance(arg, str) or not arg for arg in argv):
        raise GateFailure("Verifier commands must be explicit argv vectors")
    allowed_environment = {
        "PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "HOME", "USERPROFILE", "HOMEDRIVE",
        "HOMEPATH", "APPDATA", "LOCALAPPDATA", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "PYTHONPATH",
        "PYTHONUTF8", "PYTHONIOENCODING", "PYTHONDONTWRITEBYTECODE", "PIP_NO_INDEX", "UV_OFFLINE",
        "TEMP", "TMP", "TMPDIR",
    }
    if set(environment) - allowed_environment:
        raise GateFailure("Verifier environment contains an unapproved input")
    if any(environment.get(name) != str(temp) for name in ("TEMP", "TMP", "TMPDIR")):
        raise GateFailure("Verifier temporary environment differs from its writable root")
    profile = profile_override(descriptor, candidate=candidate, checker=checker, temporary=temp)
    launched = sandbox_argv(descriptor, cwd, profile, argv)
    acl_path = temp.parent / ("acl-" + uuid.uuid4().hex + ".json")
    writable_path = temp.parent / ("temp-acl-" + uuid.uuid4().hex + ".json")
    metadata_path = temp.parent / ("parent-metadata-acl-" + uuid.uuid4().hex + ".json")
    with metadata_acl(descriptor, [candidate, checker.parent, temp],
                      metadata_path), (
        readonly_acl(descriptor, [candidate, checker.parent, *map(Path, descriptor["read_roots"])], acl_path)
    ), (
        writable_acl(descriptor, temp, writable_path)
    ):
        result = subprocess.run(launched, cwd=cwd, env=environment, capture_output=True, timeout=timeout,
                                check=False, creationflags=0x08000000)
        # Retain output before ACL cleanup: a restoration failure must not erase
        # the subprocess evidence or turn it into a synthetic successful check.
        acl_path.with_suffix(".stdout.log").write_bytes(result.stdout)
        acl_path.with_suffix(".stderr.log").write_bytes(result.stderr)
    validate_descriptor(descriptor)
    return {
        "argv": argv, "launched_argv": launched, "cwd": str(cwd), "exit_code": result.returncode,
        "stdout": result.stdout.decode("utf-8", errors="replace"),
        "stderr": result.stderr.decode("utf-8", errors="replace"),
        "profile": profile, "profile_sha256": digest(profile.encode()),
        "descriptor_sha256": digest(encode(descriptor)),
        "readonly_acl": {"path": str(acl_path), "sha256": file_hash(acl_path)},
        "writable_temp_acl": {"path": str(writable_path), "sha256": file_hash(writable_path)},
        "parent_metadata_acl": {"path": str(metadata_path), "sha256": file_hash(metadata_path)},
    }


def _validate_descriptor_shape(descriptor: dict) -> None:
    if not isinstance(descriptor, dict) or set(descriptor) != {
        "schema", "codex", "python", "acl_helper", "denied_roots", "read_roots", "capability", "network_policy",
    } or descriptor["schema"] != SCHEMA:
        raise GateFailure("Explicit pinned native verifier sandbox descriptor required")
    if descriptor["network_policy"] not in NETWORK_POLICIES:
        raise GateFailure("Operator must explicitly select a supported verifier network policy")
    for name in ("codex", "python", "acl_helper", "capability"):
        entry = descriptor[name]
        if not isinstance(entry, dict) or set(entry) != {"path", "sha256"}:
            raise GateFailure("Invalid sandbox artifact pin")
        if file_hash(_path(entry["path"])) != entry["sha256"]:
            raise GateFailure("Sandbox artifact changed: " + name)
    if not descriptor["denied_roots"] or not descriptor["read_roots"]:
        raise GateFailure("Sandbox private roots and runtime reads must be explicit")
    denied = [_path(path) for path in descriptor["denied_roots"]]
    # The Windows account tree and all Ephy workspaces contain credentials and
    # evidence. Reopening an entire denied root would defeat that boundary.
    user_home = _path(Path.home())
    if not any(user_home == path or user_home.is_relative_to(path) for path in denied):
        raise GateFailure("Sandbox must deny the private Windows account")
    for value in descriptor["read_roots"]:
        path = _path(value)
        if any(path == private or private.is_relative_to(path) for private in denied):
            raise GateFailure("Sandbox runtime read allowance reopens a private root")


def validate_descriptor(descriptor: dict) -> dict:
    """Verify retained measured capability bytes, not a caller-supplied PASS flag."""
    _validate_descriptor_shape(descriptor)
    record = read_json(Path(descriptor["capability"]["path"]))
    if (
        record.get("schema") != "ephy.native-verifier-capability.v1"
        or record.get("codex") != descriptor["codex"]
        or record.get("python") != descriptor["python"]
        or record.get("read_roots") != descriptor["read_roots"]
        or record.get("denied_roots") != descriptor["denied_roots"]
        or record.get("network_policy") != descriptor["network_policy"]
        or record.get("acl_helper") != descriptor["acl_helper"]
        or record.get("probe_sha256") != digest(PROBE.encode("utf-8"))
        or record.get("acl_script_sha256") != digest(ACL_SCRIPT.encode("utf-8"))
        or record.get("exit_code") != 0
        or not record.get("protected_before")
        or record.get("protected_before") != record.get("protected_after")
    ):
        raise GateFailure("Native sandbox capability identity is invalid")
    stdout = Path(record["stdout"]["path"])
    stderr = Path(record["stderr"]["path"])
    if file_hash(stdout) != record["stdout"]["sha256"] or file_hash(stderr) != record["stderr"]["sha256"]:
        raise GateFailure("Native sandbox capability transcript changed")
    _validate_controls(stdout.read_text(encoding="utf-8"), record["nonce"], descriptor["network_policy"])
    expected = sandbox_argv(descriptor, Path(record["candidate"]), record["profile"], record["inner_argv"])
    if record["argv"] != expected or record["profile_sha256"] != digest(record["profile"].encode()):
        raise GateFailure("Native sandbox capability command binding changed")
    return record


def _validate_controls(stdout: str, nonce: str, network_policy: str) -> None:
    try:
        result = json.loads(stdout.strip().splitlines()[-1])
    except (ValueError, IndexError) as exc:
        raise GateFailure("Native sandbox controls did not return structured evidence") from exc
    required = CONTROL_KEYS if network_policy == "windows_all_network_denied" else (
        CONTROL_KEYS - {"loopback_denied", "child_loopback_denied"}
    )
    if (
        set(result) != {"nonce", "controls"} or result["nonce"] != nonce
        or set(result["controls"]) != CONTROL_KEYS
        or any(type(value) is not bool for value in result["controls"].values())
        or any(result["controls"][name] is not True for name in required)
    ):
        raise GateFailure("Native sandbox isolation/child/network/asyncio controls failed")


def run_controls(
    descriptor: dict, root: Path, *, candidate: Path, checker: Path,
    protected: Path | None = None,
) -> dict:
    """Repeat controls with the exact job candidate and checker read-only."""
    if os.name != "nt":
        raise GateFailure("Executable verifier profile requires native Windows isolation")
    root = _path(root, exists=False)
    root.mkdir(parents=True, exist_ok=False)
    temporary = root / "temp"
    temporary.mkdir()
    probe = root / "probe.py"
    probe.write_text(PROBE, encoding="utf-8")
    protected = protected or root / "protected-control"
    if not protected.exists():
        protected.write_bytes(uuid.uuid4().bytes)
    sibling = candidate.parent.parent / (".native-private-control-" + uuid.uuid4().hex)
    with sibling.open("xb") as stream:
        stream.write(uuid.uuid4().bytes)
    before = {str(path): file_hash(path) for path in (protected, candidate, checker, sibling)}
    profile = profile_override(
        descriptor, candidate=candidate.parent, checker=checker, temporary=temporary, extra_reads=(probe,),
    )
    nonce = uuid.uuid4().hex
    inner = [descriptor["python"]["path"], "-I", str(probe), nonce,
             str(protected), str(candidate), str(checker), str(temporary), str(sibling)]
    argv = sandbox_argv(descriptor, candidate.parent, profile, inner)
    environment = {key: os.environ[key] for key in (
        "PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "USERPROFILE", "LOCALAPPDATA", "APPDATA",
    ) if key in os.environ}
    environment.update(TEMP=str(temporary), TMP=str(temporary), TMPDIR=str(temporary), PYTHONUTF8="1")
    with metadata_acl(descriptor, [candidate.parent, checker.parent, temporary, probe],
                      root / "parent-metadata-acl.json"), readonly_acl(
        descriptor, [candidate.parent, checker.parent, probe, *map(Path, descriptor["read_roots"])],
        root / "readonly-acl.json",
    ), writable_acl(descriptor, temporary, root / "writable-temp-acl.json"):
        result = subprocess.run(argv, capture_output=True, timeout=120, env=environment, check=False,
                                creationflags=0x08000000)
        (root / "stdout.log").write_bytes(result.stdout)
        (root / "stderr.log").write_bytes(result.stderr)
    stdout, stderr = root / "stdout.log", root / "stderr.log"
    stdout.write_bytes(result.stdout)
    stderr.write_bytes(result.stderr)
    record = {
        "schema": "ephy.native-verifier-capability.v1", "codex": descriptor["codex"],
        "python": descriptor["python"], "read_roots": descriptor["read_roots"],
        "acl_helper": descriptor["acl_helper"],
        "denied_roots": descriptor["denied_roots"], "nonce": nonce,
        "network_policy": descriptor["network_policy"],
        "probe_sha256": digest(PROBE.encode("utf-8")), "candidate": str(candidate.parent),
        "acl_script_sha256": digest(ACL_SCRIPT.encode("utf-8")),
        "profile": profile, "profile_sha256": digest(profile.encode()), "argv": argv,
        "inner_argv": inner, "exit_code": result.returncode,
        "environment_sha256": digest(encode(environment)),
        "stdout": {"path": str(stdout), "sha256": file_hash(stdout)},
        "stderr": {"path": str(stderr), "sha256": file_hash(stderr)},
        "protected_before": before,
        "protected_after": {str(path): file_hash(path) for path in (protected, candidate, checker, sibling)},
    }
    write_json(root / "capability.json", record)
    if result.returncode or record["protected_before"] != record["protected_after"]:
        raise GateFailure("Native sandbox controls failed; retained evidence at " + str(root))
    _validate_controls(result.stdout.decode("utf-8"), nonce, descriptor["network_policy"])
    return record


def capture_capability(
    codex: Path, python: Path, root: Path, read_roots: list[Path],
    *, network_policy: str, denied_roots: list[Path] | None = None,
) -> dict:
    """Operator-only measurement before a contract is frozen; no model involved."""
    if network_policy not in NETWORK_POLICIES:
        raise GateFailure("Operator must explicitly select a supported verifier network policy")
    root = _path(root, exists=False)
    root.mkdir(parents=True, exist_ok=False)
    (root / "candidate").mkdir()
    (root / "checker").mkdir()
    candidate, checker = root / "candidate" / "control", root / "checker" / "control"
    candidate.write_bytes(uuid.uuid4().bytes)
    checker.write_bytes(uuid.uuid4().bytes)
    powershell = Path(os.environ["SYSTEMROOT"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    descriptor = {
        "schema": SCHEMA,
        "network_policy": network_policy,
        "codex": {"path": str(_path(codex)), "sha256": file_hash(codex)},
        "python": {"path": str(_path(python)), "sha256": file_hash(python)},
        "acl_helper": {"path": str(powershell), "sha256": file_hash(powershell)},
        "read_roots": [str(_path(path)) for path in read_roots],
        "denied_roots": [str(_path(path)) for path in (
            denied_roots or [Path.home(), Path("E:/ephy-work")]
        )],
        "capability": {"path": str(root / "measurement" / "capability.json"), "sha256": ""},
    }
    run_controls(descriptor, root / "measurement", candidate=candidate, checker=checker)
    descriptor["capability"]["sha256"] = file_hash(Path(descriptor["capability"]["path"]))
    validate_descriptor(descriptor)
    return descriptor


def identity(descriptor: dict) -> dict:
    validate_descriptor(descriptor)
    return {
        "descriptor_sha256": digest(encode(descriptor)),
        "module_sha256": file_hash(Path(__file__)),
        "codex": descriptor["codex"], "capability": descriptor["capability"],
        "acl_helper": descriptor["acl_helper"],
        "profile": PROFILE,
        "network_policy": descriptor["network_policy"],
        "loopback_isolation": "required" if descriptor["network_policy"] == "windows_all_network_denied"
        else "not guaranteed; observed separately in parent and child controls",
        "private_ancestors": "exact declared-root ancestors: metadata/traverse only; contents remain denied",
    }
