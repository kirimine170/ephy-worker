#!/usr/bin/env python3
"""Initialize a repository created from the Ephy repository template.

This script intentionally renders known placeholders instead of parsing arbitrary
YAML. It performs local file operations only: no network, GitHub, or Git remote
operation is implemented here.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from typing import Mapping, Sequence


PROJECT_TYPES = (
    "meta",
    "core",
    "model",
    "extension",
    "integration",
    "application",
    "private-instance",
    "template",
)
PROJECT_STATUSES = (
    "concept",
    "design",
    "active",
    "maintenance",
    "hold",
    "deprecated",
)
VISIBILITIES = ("private", "internal", "public")
DATA_CLASSIFICATIONS = ("public", "internal", "confidential", "restricted")

PROJECT_ID_PATTERN = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
PLACEHOLDER_PATTERN = re.compile(r"@@[A-Z0-9_]+@@")
TEMPLATE_MARKER = "<!-- ephy-template-source -->"
TEMPLATE_PROJECT_ID = "ephy-repository-template"

PROJECT_TEMPLATE_PLACEHOLDERS = {
    "@@PROJECT_ID@@",
    "@@PROJECT_TYPE@@",
    "@@PROJECT_STATUS@@",
    "@@PROJECT_VISIBILITY@@",
    "@@PROJECT_DESCRIPTION@@",
    "@@PROJECT_PARENT@@",
    "@@DEPENDS_ON@@",
    "@@INTEGRATES_WITH@@",
    "@@RUNS_ON@@",
    "@@DATA_CLASSIFICATION@@",
}
README_TEMPLATE_PLACEHOLDERS = {
    "@@PROJECT_ID@@",
    "@@PROJECT_DESCRIPTION@@",
    "@@PROJECT_TYPE@@",
    "@@PROJECT_STATUS@@",
    "@@PROJECT_VISIBILITY@@",
    "@@PROJECT_PARENT@@",
    "@@DEPENDS_ON_MARKDOWN@@",
    "@@INTEGRATES_WITH_MARKDOWN@@",
    "@@RUNS_ON_MARKDOWN@@",
    "@@DATA_CLASSIFICATION@@",
}


class InitializationError(ValueError):
    """Raised when initialization cannot proceed safely."""


def repository_root(explicit_root: str | None = None) -> Path:
    """Resolve the target root without relying on the caller's working directory."""
    root = (
        Path(explicit_root).expanduser()
        if explicit_root is not None
        else Path(__file__).resolve().parents[1]
    ).resolve()
    if not root.is_dir():
        raise InitializationError(f"repository root is not a directory: {root}")
    return root


def ensure_safe_path(root: Path, path: Path) -> None:
    """Reject paths that escape the resolved repository root through a symlink."""
    try:
        path.resolve().relative_to(root)
    except ValueError as exc:
        raise InitializationError(f"path escapes repository root: {path}") from exc


def validate_identifier(value: str, label: str) -> str:
    if not PROJECT_ID_PATTERN.fullmatch(value):
        raise InitializationError(
            f"{label} must be lowercase kebab-case and start with a letter: {value!r}"
        )
    return value


def validate_text(value: str, label: str) -> str:
    value = value.strip()
    if not value:
        raise InitializationError(f"{label} must not be empty")
    if any(character in value for character in ("\n", "\r", "\x00")):
        raise InitializationError(f"{label} must be a single line")
    return value


def validate_relation_list(values: Sequence[str], label: str) -> list[str]:
    validated = [validate_identifier(value, label) for value in values]
    if len(validated) != len(set(validated)):
        raise InitializationError(f"{label} must not contain duplicate project IDs")
    return validated


def render_known_template(
    source: str,
    replacements: Mapping[str, str],
    expected_placeholders: set[str],
    source_name: str,
) -> str:
    """Replace a fixed placeholder vocabulary and reject template drift."""
    found = set(PLACEHOLDER_PATTERN.findall(source))
    missing = expected_placeholders - found
    unexpected = found - expected_placeholders
    if missing or unexpected:
        details = []
        if missing:
            details.append(f"missing {sorted(missing)}")
        if unexpected:
            details.append(f"unexpected {sorted(unexpected)}")
        raise InitializationError(f"invalid placeholders in {source_name}: {'; '.join(details)}")

    rendered = source
    for placeholder, replacement in replacements.items():
        rendered = rendered.replace(placeholder, replacement)
    remaining = PLACEHOLDER_PATTERN.findall(rendered)
    if remaining:
        raise InitializationError(
            f"unreplaced placeholders in {source_name}: {sorted(set(remaining))}"
        )
    return rendered


def markdown_relation_list(values: Sequence[str]) -> str:
    if not values:
        return "  - None declared．"
    return "\n".join(f"  - `{value}`" for value in values)


def is_template_identity(metadata: str, readme: str | None) -> bool:
    id_match = re.search(
        r'^  id:\s*(?:"ephy-repository-template"|ephy-repository-template)\s*$',
        metadata,
        re.MULTILINE,
    )
    type_match = re.search(
        r'^  type:\s*(?:"template"|template)\s*$', metadata, re.MULTILINE
    )
    readme_is_template = readme is None or TEMPLATE_MARKER in readme
    return bool(id_match and type_match and readme_is_template)


def write_atomic(path: Path, content: str) -> None:
    """Replace one UTF-8 text file without exposing a partially written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary.write(content)
            temporary_name = temporary.name
        os.replace(temporary_name, path)
    finally:
        if temporary_name is not None:
            temporary_path = Path(temporary_name)
            if temporary_path.exists():
                temporary_path.unlink()


def initialize(root: Path, args: argparse.Namespace) -> None:
    project_id = validate_identifier(args.project_id, "project ID")
    parent = None if args.no_parent else validate_identifier(args.parent, "parent")
    if parent == project_id:
        raise InitializationError("parent must not reference the project itself")
    if parent is None and args.project_type != "meta":
        raise InitializationError("--no-parent is reserved for an ecosystem root meta project")
    description = validate_text(args.description, "description")
    depends_on = validate_relation_list(args.depends_on, "depends_on")
    integrates_with = validate_relation_list(
        args.integrates_with, "integrates_with"
    )
    runs_on = validate_relation_list(args.runs_on, "runs_on")

    project_template_path = root / ".ephy" / "project.template.yaml"
    readme_template_path = root / "README.template.md"
    project_path = root / ".ephy" / "project.yaml"
    readme_path = root / "README.md"
    for path in (
        project_template_path,
        readme_template_path,
        project_path,
        readme_path,
    ):
        ensure_safe_path(root, path)

    for path in (project_template_path, readme_template_path):
        if not path.is_file():
            raise InitializationError(f"required template file is missing: {path}")

    existing_metadata = (
        project_path.read_text(encoding="utf-8") if project_path.is_file() else None
    )
    existing_readme = (
        readme_path.read_text(encoding="utf-8") if readme_path.is_file() else None
    )
    targets_exist = existing_metadata is not None or existing_readme is not None
    pristine_template = existing_metadata is not None and is_template_identity(
        existing_metadata, existing_readme
    )
    if targets_exist and not pristine_template and not args.force:
        raise InitializationError(
            "repository is already initialized or contains non-template target files; "
            "rerun with --force only if replacing .ephy/project.yaml and README.md is intended"
        )

    project_source = project_template_path.read_text(encoding="utf-8")
    readme_source = readme_template_path.read_text(encoding="utf-8")
    yaml_replacements = {
        '"@@PROJECT_ID@@"': json.dumps(project_id, ensure_ascii=False),
        '"@@PROJECT_TYPE@@"': json.dumps(args.project_type, ensure_ascii=False),
        '"@@PROJECT_STATUS@@"': json.dumps(args.status, ensure_ascii=False),
        '"@@PROJECT_VISIBILITY@@"': json.dumps(args.visibility, ensure_ascii=False),
        '"@@PROJECT_DESCRIPTION@@"': json.dumps(description, ensure_ascii=False),
        '"@@PROJECT_PARENT@@"': json.dumps(parent, ensure_ascii=False),
        '"@@DEPENDS_ON@@"': json.dumps(depends_on, ensure_ascii=False),
        '"@@INTEGRATES_WITH@@"': json.dumps(integrates_with, ensure_ascii=False),
        '"@@RUNS_ON@@"': json.dumps(runs_on, ensure_ascii=False),
        '"@@DATA_CLASSIFICATION@@"': json.dumps(
            args.classification, ensure_ascii=False
        ),
    }
    readme_replacements = {
        "@@PROJECT_ID@@": project_id,
        "@@PROJECT_DESCRIPTION@@": description,
        "@@PROJECT_TYPE@@": args.project_type,
        "@@PROJECT_STATUS@@": args.status,
        "@@PROJECT_VISIBILITY@@": args.visibility,
        "@@PROJECT_PARENT@@": (
            "None — ecosystem root" if parent is None else parent
        ),
        "@@DEPENDS_ON_MARKDOWN@@": markdown_relation_list(depends_on),
        "@@INTEGRATES_WITH_MARKDOWN@@": markdown_relation_list(integrates_with),
        "@@RUNS_ON_MARKDOWN@@": markdown_relation_list(runs_on),
        "@@DATA_CLASSIFICATION@@": args.classification,
    }
    project_content = render_known_template(
        project_source,
        yaml_replacements,
        PROJECT_TEMPLATE_PLACEHOLDERS,
        str(project_template_path.relative_to(root)),
    )
    readme_content = render_known_template(
        readme_source,
        readme_replacements,
        README_TEMPLATE_PLACEHOLDERS,
        str(readme_template_path.relative_to(root)),
    )

    write_atomic(project_path, project_content)
    write_atomic(readme_path, readme_content)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Initialize a local repository created from the Ephy template."
    )
    parser.add_argument("--id", dest="project_id", required=True)
    parser.add_argument("--type", dest="project_type", choices=PROJECT_TYPES, required=True)
    parser.add_argument("--description", required=True)
    parser.add_argument("--status", choices=PROJECT_STATUSES, default="active")
    parser.add_argument("--visibility", choices=VISIBILITIES, default="private")
    parser.add_argument("--classification", choices=DATA_CLASSIFICATIONS, default="internal")
    parent_group = parser.add_mutually_exclusive_group()
    parent_group.add_argument("--parent", default="ephy")
    parent_group.add_argument(
        "--no-parent",
        action="store_true",
        help="set relations.parent to null for an ecosystem root meta project",
    )
    parser.add_argument("--depends-on", action="append", default=[], metavar="PROJECT_ID")
    parser.add_argument(
        "--integrates-with", action="append", default=[], metavar="PROJECT_ID"
    )
    parser.add_argument("--runs-on", action="append", default=[], metavar="PROJECT_ID")
    parser.add_argument(
        "--force",
        action="store_true",
        help="replace an already initialized project.yaml and README.md",
    )
    parser.add_argument(
        "--root",
        help=argparse.SUPPRESS,
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        root = repository_root(args.root)
        initialize(root, args)
    except (InitializationError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"Initialized {args.project_id} in {root}")
    print("Review .ephy/project.yaml and README.md, then run validate_repository.py．")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
