"""
Custom Test Runner and Verification Reporter for Moodle Synchronizer Suite.
Executes all test tiers (1-4) across features F1-F19, aggregates results,
and outputs a clean structured verification summary.
"""

import sys
import time
import unittest
from pathlib import Path

# Add project root to sys.path
ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))


def run_full_suite():
    loader = unittest.TestLoader()
    suite = loader.discover(start_dir=str(Path(__file__).parent), pattern="test_*.py")

    print("=" * 80)
    print(" MOODLE SYNCHRONIZER TEST SUITE RUNNER")
    print(" Covers Features F1 - F19 across Tiers 1 - 4 per TEST_INFRA.md")
    print("=" * 80)

    start_time = time.time()
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    elapsed = time.time() - start_time

    print("\n" + "=" * 80)
    print(f" EXECUTION SUMMARY: {result.testsRun} tests run in {elapsed:.3f}s")
    print(f" PASSED: {result.testsRun - len(result.failures) - len(result.errors) - len(result.skipped)}")
    print(f" FAILED: {len(result.failures)}")
    print(f" ERRORS: {len(result.errors)}")
    print(f" SKIPPED: {len(result.skipped)}")
    print("=" * 80)

    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(run_full_suite())
