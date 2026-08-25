from __future__ import annotations

from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
VALIDATE_SCRIPT = REPOSITORY_ROOT / "scripts" / "validate_repository.py"
sys.path.insert(0, str(REPOSITORY_ROOT / "scripts"))

from validate_repository import validate_parent_graph


class ValidateRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.repository = Path(self.temporary_directory.name) / "repository"
        shutil.copytree(
            REPOSITORY_ROOT,
            self.repository,
            ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc"),
        )

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def run_validator(
        self, *arguments: str
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                str(VALIDATE_SCRIPT),
                "--root",
                str(self.repository),
                *arguments,
            ],
            text=True,
            capture_output=True,
            check=False,
        )

    def test_template_repository_is_valid(self) -> None:
        result = self.run_validator("--check-sensitive-patterns")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_invalid_metadata_enum_is_detected(self) -> None:
        metadata_path = self.repository / ".ephy" / "project.yaml"
        metadata = metadata_path.read_text(encoding="utf-8")
        metadata_path.write_text(
            re.sub(r'^  type: .+$', '  type: "unknown"', metadata, count=1, flags=re.MULTILINE),
            encoding="utf-8",
        )
        result = self.run_validator()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unsupported project.type", result.stderr)

    def test_invalid_data_policy_is_detected(self) -> None:
        metadata_path = self.repository / ".ephy" / "project.yaml"
        metadata = metadata_path.read_text(encoding="utf-8")
        metadata_path.write_text(
            metadata.replace(
                'personal_data_in_git: "prohibited"',
                'personal_data_in_git: "allowed"',
            ),
            encoding="utf-8",
        )
        result = self.run_validator()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("personal_data_in_git must be prohibited", result.stderr)

    def test_self_parent_is_detected(self) -> None:
        metadata_path = self.repository / ".ephy" / "project.yaml"
        metadata = metadata_path.read_text(encoding="utf-8")
        project_id = re.search(r'^  id: "([^"]+)"$', metadata, re.MULTILINE)
        self.assertIsNotNone(project_id)
        metadata_path.write_text(
            re.sub(
                r'^  parent: .+$',
                f'  parent: "{project_id.group(1)}"',
                metadata,
                count=1,
                flags=re.MULTILINE,
            ),
            encoding="utf-8",
        )
        result = self.run_validator()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("must not reference itself", result.stderr)

    def test_parent_cycle_is_detected(self) -> None:
        errors = validate_parent_graph(
            {"ephy-a": "ephy-b", "ephy-b": "ephy-c", "ephy-c": "ephy-a"}
        )
        self.assertEqual(
            errors,
            ["parent graph: cycle detected: ephy-a -> ephy-b -> ephy-c -> ephy-a"],
        )

    def test_missing_readme_section_is_detected(self) -> None:
        readme_path = self.repository / "README.md"
        readme = readme_path.read_text(encoding="utf-8")
        readme_path.write_text(
            readme.replace("## License", "## Licensing notes"), encoding="utf-8"
        )
        result = self.run_validator()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("missing required section 'License'", result.stderr)

    def test_schema_drift_is_detected(self) -> None:
        schema_path = self.repository / ".ephy" / "schema" / "project.schema.json"
        schema = schema_path.read_text(encoding="utf-8")
        schema_path.write_text(
            schema.replace('"private-instance",', '"private-service",'),
            encoding="utf-8",
        )
        result = self.run_validator()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("project.type enum differs from tooling", result.stderr)

    def test_unintended_placeholder_is_detected(self) -> None:
        architecture_path = self.repository / "docs" / "architecture.md"
        content = architecture_path.read_text(encoding="utf-8")
        unintended = "@@" + "UNINTENDED_VALUE" + "@@"
        architecture_path.write_text(content + "\n" + unintended + "\n", encoding="utf-8")
        result = self.run_validator()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unexpected placeholders", result.stderr)

    def test_sensitive_fixed_value_is_detected_when_enabled(self) -> None:
        suspicious_path = self.repository / "suspicious.txt"
        suspicious_path.write_text("AKIA" + ("A" * 16) + "\n", encoding="utf-8")
        result = self.run_validator("--check-sensitive-patterns")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("AWS access key", result.stderr)


if __name__ == "__main__":
    unittest.main()
