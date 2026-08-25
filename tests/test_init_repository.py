from __future__ import annotations

import re
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
INIT_SCRIPT = REPOSITORY_ROOT / "scripts" / "init_repository.py"
VALIDATE_SCRIPT = REPOSITORY_ROOT / "scripts" / "validate_repository.py"


class InitRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.repository = Path(self.temporary_directory.name) / "repository"
        shutil.copytree(
            REPOSITORY_ROOT,
            self.repository,
            ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc"),
        )
        (self.repository / ".ephy" / "project.yaml").write_text(
            """schema_version: 1

project:
  id: "ephy-repository-template"
  family: "ephy"
  type: "template"
  status: "active"
  visibility: "private"
  description: "Test template identity"

relations:
  parent: "ephy"
  depends_on: []
  integrates_with: []
  runs_on: []

data_policy:
  classification: "internal"
  personal_data_in_git: "prohibited"
  secrets_in_git: "prohibited"
""",
            encoding="utf-8",
        )
        (self.repository / "README.md").write_text(
            "<!-- ephy-template-source -->\n# Ephy Repository Template\n",
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def run_init(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                str(INIT_SCRIPT),
                "--root",
                str(self.repository),
                *arguments,
            ],
            text=True,
            capture_output=True,
            check=False,
        )

    def valid_arguments(self, project_id: str = "ephy-cam") -> list[str]:
        return [
            "--id",
            project_id,
            "--type",
            "extension",
            "--description",
            "Camera and visual-ingestion infrastructure for Ephy",
            "--visibility",
            "private",
            "--classification",
            "restricted",
            "--depends-on",
            "ephy-runtime",
            "--integrates-with",
            "ephy-worker",
            "--runs-on",
            "ephy-device",
        ]

    def test_valid_arguments_initialize_and_validate(self) -> None:
        result = self.run_init(*self.valid_arguments())
        self.assertEqual(result.returncode, 0, result.stderr)

        metadata = (self.repository / ".ephy" / "project.yaml").read_text(
            encoding="utf-8"
        )
        readme = (self.repository / "README.md").read_text(encoding="utf-8")
        self.assertIn('id: "ephy-cam"', metadata)
        self.assertIn('depends_on: ["ephy-runtime"]', metadata)
        self.assertIn("# ephy-cam", readme)
        self.assertIn("`ephy-worker`", readme)
        self.assertIsNone(re.search(r"@@[A-Z0-9_]+@@", metadata))
        self.assertIsNone(re.search(r"@@[A-Z0-9_]+@@", readme))

        validation = subprocess.run(
            [
                sys.executable,
                str(VALIDATE_SCRIPT),
                "--root",
                str(self.repository),
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(validation.returncode, 0, validation.stderr)

    def test_invalid_project_id_is_rejected_without_changes(self) -> None:
        metadata_path = self.repository / ".ephy" / "project.yaml"
        before = metadata_path.read_bytes()
        result = self.run_init(*self.valid_arguments("Ephy_Cam"))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("lowercase kebab-case", result.stderr)
        self.assertEqual(metadata_path.read_bytes(), before)

    def test_unsupported_type_is_rejected_by_argument_parser(self) -> None:
        metadata_path = self.repository / ".ephy" / "project.yaml"
        before = metadata_path.read_bytes()
        arguments = self.valid_arguments()
        arguments[arguments.index("extension")] = "service"
        result = self.run_init(*arguments)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("invalid choice", result.stderr)
        self.assertEqual(metadata_path.read_bytes(), before)

    def test_root_meta_project_can_be_initialized_without_parent(self) -> None:
        arguments = self.valid_arguments("ephy")
        arguments[arguments.index("extension")] = "meta"
        result = self.run_init(*arguments, "--no-parent")
        self.assertEqual(result.returncode, 0, result.stderr)

        metadata = (self.repository / ".ephy" / "project.yaml").read_text(
            encoding="utf-8"
        )
        readme = (self.repository / "README.md").read_text(encoding="utf-8")
        self.assertIn("parent: null", metadata)
        self.assertIn("None — ecosystem root", readme)

        validation = subprocess.run(
            [
                sys.executable,
                str(VALIDATE_SCRIPT),
                "--root",
                str(self.repository),
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(validation.returncode, 0, validation.stderr)

    def test_parent_and_no_parent_are_mutually_exclusive(self) -> None:
        arguments = self.valid_arguments("ephy")
        arguments[arguments.index("extension")] = "meta"
        result = self.run_init(
            *arguments, "--parent", "ephy-root", "--no-parent"
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not allowed with argument", result.stderr)

    def test_self_parent_is_rejected(self) -> None:
        result = self.run_init(*self.valid_arguments("ephy"))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("must not reference the project itself", result.stderr)

    def test_reinitialization_requires_force(self) -> None:
        first = self.run_init(*self.valid_arguments("ephy-first"))
        self.assertEqual(first.returncode, 0, first.stderr)

        second = self.run_init(*self.valid_arguments("ephy-second"))
        self.assertNotEqual(second.returncode, 0)
        metadata_path = self.repository / ".ephy" / "project.yaml"
        self.assertIn('id: "ephy-first"', metadata_path.read_text(encoding="utf-8"))

        forced = self.run_init(*self.valid_arguments("ephy-second"), "--force")
        self.assertEqual(forced.returncode, 0, forced.stderr)
        self.assertIn('id: "ephy-second"', metadata_path.read_text(encoding="utf-8"))

    def test_initializing_copy_does_not_modify_source_templates(self) -> None:
        source_project_template = (
            REPOSITORY_ROOT / ".ephy" / "project.template.yaml"
        )
        source_readme_template = REPOSITORY_ROOT / "README.template.md"
        before = (
            source_project_template.read_bytes(),
            source_readme_template.read_bytes(),
        )
        result = self.run_init(*self.valid_arguments())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            before,
            (
                source_project_template.read_bytes(),
                source_readme_template.read_bytes(),
            ),
        )


if __name__ == "__main__":
    unittest.main()
