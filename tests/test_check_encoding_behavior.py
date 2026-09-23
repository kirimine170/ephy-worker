"""Mutation probes for the fixed Windows encoding acceptance definition."""

from __future__ import annotations

import runpy
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CANONICAL = runpy.run_path(str(ROOT / "scripts" / "check_encoding_behavior.py"))["canonical"]
SOURCE = '''def test_cli_help_and_missing_profile(tmp_path):
    result = subprocess.run(
        [sys.executable, "-m", "ephy_worker", "--help"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0 and "research" in result.stdout and "doctor" in result.stdout
    result = subprocess.run(
        [sys.executable, "-m", "ephy_worker", "research", "--config", str(fixture_config(tmp_path))],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 1
    assert not (tmp_path / "out").exists()
'''


class FixedEncodingDefinitionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.baseline = Path(self.temp.name) / "baseline.py"
        self.candidate = Path(self.temp.name) / "candidate.py"
        self.source = SOURCE
        self.baseline.write_text(self.source, encoding="utf-8")

    def compare(self, updated: str) -> bool:
        self.candidate.write_text(updated, encoding="utf-8")
        return CANONICAL(self.baseline, candidate=False) == CANONICAL(self.candidate, candidate=True)

    def test_explicit_utf8_on_both_probes_is_allowed(self) -> None:
        updated = self.source.replace("        text=True,\n", '        text=True,\n        encoding="utf-8",\n', 2)
        self.assertTrue(self.compare(updated))

    def test_encoding_keyword_can_replace_text_true(self) -> None:
        updated = self.source.replace("        text=True,\n", '        encoding="utf-8",\n', 2)
        self.assertTrue(self.compare(updated))

    def test_binary_capture_with_strict_full_decode_is_allowed(self) -> None:
        updated = self.source.replace("        text=True,\n", "        text=False,\n", 2)
        updated = updated.replace(
            '    assert result.returncode == 0 and "research" in result.stdout and "doctor" in result.stdout\n',
            '    stdout_help = result.stdout.decode("utf-8")\n'
            '    assert result.returncode == 0 and "research" in stdout_help and "doctor" in stdout_help\n',
            1,
        )
        updated = updated.replace(
            "    assert result.returncode == 1\n",
            '    stdout_missing = result.stdout.decode("utf-8")\n    assert result.returncode == 1\n',
            1,
        )
        self.assertTrue(self.compare(updated))

    def test_binary_capture_with_decode_error_suppression_is_rejected(self) -> None:
        updated = self.source.replace("        text=True,\n", "        text=False,\n", 1)
        updated = updated.replace(
            '    assert result.returncode == 0 and "research" in result.stdout and "doctor" in result.stdout\n',
            '    stdout_help = result.stdout.decode("utf-8", errors="ignore")\n'
            '    assert result.returncode == 0 and "research" in stdout_help and "doctor" in stdout_help\n',
            1,
        )
        self.candidate.write_text(updated, encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "strict UTF-8"):
            CANONICAL(self.candidate, candidate=True)

    def test_removed_assertion_is_rejected(self) -> None:
        self.assertFalse(self.compare(self.source.replace("    assert result.returncode == 1\n", "", 1)))

    def test_skipped_test_is_rejected(self) -> None:
        self.assertFalse(
            self.compare(self.source.replace("def test_cli_help_and_missing_profile", "@pytest.mark.skip\ndef test_cli_help_and_missing_profile", 1))
        )

    def test_decode_error_suppression_is_rejected(self) -> None:
        updated = self.source.replace("        text=True,\n", '        text=True,\n        errors="ignore",\n', 1)
        self.candidate.write_text(updated, encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "hide a decode failure"):
            CANONICAL(self.candidate, candidate=True)

    def test_content_loss_is_rejected(self) -> None:
        self.assertFalse(self.compare(self.source.replace("result.stdout and", "result.stdout[:5] and", 1)))


if __name__ == "__main__":
    unittest.main()
