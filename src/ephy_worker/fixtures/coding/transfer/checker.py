"""Run fixed tests from outside a synthetic candidate; this is not a sandbox."""

from __future__ import annotations

import io
import json
import sys
import unittest
from pathlib import Path


def main() -> int:
    candidate, tests = (Path(value).resolve() for value in sys.argv[1:3])
    # -I -B prevents inherited Python startup paths and bytecode writes.
    # Only the candidate's implementation is imported; tests live outside it.
    sys.path.insert(0, str(candidate))
    suite = unittest.defaultTestLoader.discover(str(tests), pattern="test_*.py")
    stream = io.StringIO()
    result = unittest.TextTestRunner(stream=stream).run(suite)
    payload = {
        "tests_run": result.testsRun,
        "failures": len(result.failures),
        "errors": len(result.errors),
        "skipped": len(result.skipped),
        "expected_failures": len(result.expectedFailures),
        "unexpected_successes": len(result.unexpectedSuccesses),
    }
    print(json.dumps(payload, sort_keys=True))
    if not result.wasSuccessful():
        print(stream.getvalue(), file=sys.stderr)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
