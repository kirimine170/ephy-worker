"""Read-only checks before reviewing a patch made from a working-tree snapshot．"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .coding_executor import CodingExecutionError, resolve_repository
from .coding_schema import CodingResult, safe_relative_path


def verify_source_manifest(artifact_dir: Path, repository_path: Path) -> dict:
    """Check that the source checkout still matches the candidate's baseline．"""

    try:
        result = CodingResult.model_validate_json(
            (artifact_dir / "result.json").read_text(encoding="utf-8")
        )
        if result.repository.source_state != "working-tree":
            raise ValueError("result was not created from a working-tree snapshot")
        if result.artifacts.source_manifest != "source-manifest.json":
            raise ValueError("source manifest is missing from result")
        manifest = json.loads((artifact_dir / "source-manifest.json").read_text(encoding="utf-8"))
        if (
            manifest["source_revision"] != result.repository.source_revision
            or manifest["snapshot_revision"] != result.repository.base_revision
        ):
            raise ValueError("source manifest does not match result")
        files = manifest["files"]
        if not isinstance(files, list):
            raise TypeError("invalid source manifest files")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise CodingExecutionError(
            "repository_unavailable", "repository", f"invalid candidate artifacts ({type(exc).__name__})"
        ) from None

    repository, head = resolve_repository(repository_path, "HEAD")
    changed: list[str] = []
    missing: list[str] = []
    baseline_paths: set[str] = set()
    for item in files:
        try:
            name = safe_relative_path(item["path"])
            expected_hash = item["sha256"]
            expected_size = item["bytes"]
            if not isinstance(expected_hash, str) or not isinstance(expected_size, int):
                raise TypeError
            baseline_paths.add(name)
            path = repository.joinpath(*name.split("/"))
            if not path.resolve().is_relative_to(repository):
                raise ValueError
            data = path.read_bytes()
        except (OSError, ValueError, KeyError, TypeError):
            missing.append(item.get("path", "<invalid>") if isinstance(item, dict) else "<invalid>")
            continue
        if len(data) != expected_size or hashlib.sha256(data).hexdigest() != expected_hash:
            changed.append(name)
    conflicts = [
        name
        for name in result.changed_files
        if name not in baseline_paths and repository.joinpath(*safe_relative_path(name).split("/")).exists()
    ]
    revision_matches = head == manifest["source_revision"]
    return {
        "ok": revision_matches and not changed and not missing and not conflicts,
        "source_revision_matches": revision_matches,
        "changed_files": changed,
        "missing_files": missing,
        "conflicting_new_files": conflicts,
    }
