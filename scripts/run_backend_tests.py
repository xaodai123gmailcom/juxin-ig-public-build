#!/usr/bin/env python3
"""Run the complete Core test suite from a source checkout.

The packaged Core imports ``app`` from the backend source root.  Adding that
same root here makes test discovery behave identically whether it is launched
from npm, PowerShell, Windows Explorer, or the repository root.
"""

from __future__ import annotations

import argparse
import faulthandler
import math
import os
import sys
import time
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = PROJECT_ROOT / "backend"
TEST_ROOT = BACKEND_ROOT / "tests"


class TimedTestResult(unittest.TextTestResult):
    """Bound each real test, including setup/cleanup, independently of suite size."""

    case_timeout = 180.0

    def startTest(self, test):
        self.case_started = time.monotonic()
        print(f"CHECK backend case {self.testsRun + 1}: {test.id()}", flush=True)
        faulthandler.dump_traceback_later(self.case_timeout, exit=True)
        super().startTest(test)

    def stopTest(self, test):
        super().stopTest(test)
        print(f"CHECK backend case finished: {test.id()} "
              f"({time.monotonic() - self.case_started:.1f}s)", flush=True)
        # Also bound between-test/class cleanup and final interpreter shutdown.
        faulthandler.dump_traceback_later(self.case_timeout, exit=True)

for import_root in (PROJECT_ROOT, BACKEND_ROOT, TEST_ROOT):
    if str(import_root) not in sys.path:
        sys.path.insert(0, str(import_root))


def _iter_test_cases(suite: unittest.TestSuite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from _iter_test_cases(item)
        else:
            yield item


def _test_case_shards(
    suite: unittest.TestSuite,
    shard_count: int,
) -> list[list[unittest.TestCase]]:
    """Assign every discovered test exactly once across deterministic shards."""

    test_cases = sorted(_iter_test_cases(suite), key=lambda case: case.id())
    if not test_cases:
        raise RuntimeError(f"no backend tests found below {TEST_ROOT}")
    if shard_count < 1 or shard_count > len(test_cases):
        raise ValueError(
            f"shard count must be between 1 and {len(test_cases)}, got {shard_count}"
        )
    if len({case.id() for case in test_cases}) != len(test_cases):
        raise RuntimeError("backend test discovery returned duplicate test identifiers")

    shards = [test_cases[index::shard_count] for index in range(shard_count)]
    assigned_ids = sorted(case.id() for shard in shards for case in shard)
    if assigned_ids != [case.id() for case in test_cases]:
        raise RuntimeError("backend test sharding lost or duplicated a test case")
    return shards


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-p", "--pattern", default="test*.py",
                        help="Test filename pattern, relative to backend/tests")
    parser.add_argument("-v", "--verbose", dest="verbosity", action="store_const",
                        const=2, default=2, help="Show individual test results (default)")
    parser.add_argument("--shard-index", type=int)
    parser.add_argument("--shard-count", type=int, default=1)
    parser.add_argument("--case-timeout", type=float,
                        default=os.environ.get("IGAC_TEST_CASE_TIMEOUT_SECONDS"),
                        help="Optional per-test/setup/cleanup deadline in seconds; dumps stacks on expiry")
    args = parser.parse_args()
    if not args.pattern or "/" in args.pattern or "\\" in args.pattern:
        parser.error("--pattern must be a filename pattern, not a path")
    if args.case_timeout is not None and (not math.isfinite(args.case_timeout) or args.case_timeout <= 0):
        parser.error("--case-timeout must be finite and positive")
    return args


def main() -> int:
    args = _parse_args()
    if args.case_timeout is not None:
        faulthandler.enable()
        faulthandler.dump_traceback_later(args.case_timeout, exit=True)
        print(f"CHECK backend discovery (individual phase limit {args.case_timeout:g}s)", flush=True)
    discovered = unittest.defaultTestLoader.discover(
        start_dir=str(TEST_ROOT),
        pattern=args.pattern,
    )
    if discovered.countTestCases() == 0:
        raise SystemExit(f"No backend tests match pattern: {args.pattern}")
    if args.shard_index is None:
        if args.shard_count != 1:
            raise SystemExit("--shard-count requires --shard-index")
        suite = discovered
    else:
        shards = _test_case_shards(discovered, args.shard_count)
        if args.shard_index < 0 or args.shard_index >= len(shards):
            raise SystemExit(
                f"--shard-index must be between 0 and {len(shards) - 1}"
            )
        selected = shards[args.shard_index]
        print(
            f"Backend test shard {args.shard_index + 1}/{args.shard_count}: "
            f"{len(selected)} of {sum(len(shard) for shard in shards)} tests"
        )
        suite = unittest.TestSuite(selected)
    result_class = unittest.TextTestResult
    if args.case_timeout is not None:
        class ConfiguredResult(TimedTestResult):
            case_timeout = args.case_timeout
        result_class = ConfiguredResult
    result = unittest.TextTestRunner(verbosity=args.verbosity, resultclass=result_class).run(suite)
    # Keep the watchdog armed until the child exits, so orphaned threads after
    # an apparent OK summary cannot make the parent wait for its overall cap.
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
