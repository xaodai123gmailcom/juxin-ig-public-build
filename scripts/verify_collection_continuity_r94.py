#!/usr/bin/env python3
"""Twenty reproducible offline collection audits; real scheduler and SQLite.

Each round starts a fresh Python process, varies test order and the seeded
stream's batch sizes/order/child count/latency, and runs every listed scenario.
No live Instagram traffic or Windows UI is exercised by this audit.
"""
from __future__ import annotations

import argparse
import faulthandler
import json
import os
from pathlib import Path
import random
import subprocess
import sys
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
MODULES = [
    "test_collection_continuity_r94", "test_collection_preflight_r94",
    "test_collection_faults_r94", "test_parallel_relation_pipeline",
    "test_durable_cancellation_barrier", "test_checkpoint_recovery_r25",
    "test_preopen_dedupe_r39", "test_recovery_runtime_r25",
    "test_collection_drain_r56", "test_completion_persistence_r44",
    "test_spool_performance_r30", "test_collection_long_run",
    "test_source_profile_snapshot_r56",
]


def cases(suite):
    for value in suite:
        if isinstance(value, unittest.TestSuite):
            yield from cases(value)
        else:
            yield value


class BoundedResult(unittest.TextTestResult):
    def startTest(self, test):
        faulthandler.dump_traceback_later(60, exit=True)
        super().startTest(test)

    def stopTest(self, test):
        super().stopTest(test)
        faulthandler.dump_traceback_later(60, exit=True)


def run_round(seed, destination):
    sys.path[:0] = [str(ROOT / "backend"), str(ROOT / "backend/tests")]
    os.environ["R94_AUDIT_SEED"] = str(seed)
    selected = list(cases(unittest.defaultTestLoader.loadTestsFromNames(MODULES)))
    ids = [case.id() for case in selected]
    if len(ids) != len(set(ids)):
        raise RuntimeError("Duplicate tests in audit")
    random.Random(seed).shuffle(selected)
    started = time.monotonic()
    result = unittest.TextTestRunner(verbosity=2, resultclass=BoundedResult, failfast=True).run(
        unittest.TestSuite(selected))
    faulthandler.cancel_dump_traceback_later()
    report = {"seed": seed, "tests": result.testsRun, "expected_tests": len(ids),
              "seconds": round(time.monotonic() - started, 3),
              "passed": result.wasSuccessful() and not result.skipped and result.testsRun == len(ids),
              "failures": len(result.failures), "errors": len(result.errors),
              "skipped": len(result.skipped), "test_order": [case.id() for case in selected]}
    destination.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return 0 if report["passed"] else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rounds", type=int, default=20)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--seed-start", type=int, default=1)
    parser.add_argument("--round", type=int, help=argparse.SUPPRESS)
    args = parser.parse_args()
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    if args.round is not None:
        return run_round(args.round, output / f"round-{args.round:02d}.json")
    if args.rounds < 1:
        parser.error("--rounds must be positive")
    reports = []
    for seed in range(args.seed_start, args.seed_start + args.rounds):
        print(f"ROUND {len(reports)+1}/{args.rounds} seed={seed} START", flush=True)
        env = {**os.environ, "PYTHONHASHSEED": str(seed), "R94_AUDIT_SEED": str(seed)}
        report_path = output / f"round-{seed:02d}.json"
        report_path.unlink(missing_ok=True)
        try:
            with (output / f"round-{seed:02d}.log").open("wb") as log:
                completed = subprocess.run([sys.executable, "-X", "utf8", str(Path(__file__).resolve()),
                    "--round", str(seed), "--output-dir", str(output)],
                    cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT, timeout=600)
            code = completed.returncode
        except subprocess.TimeoutExpired:
            code = 124
        report = json.loads(report_path.read_text(encoding="utf-8")) if report_path.exists() else {
            "seed": seed, "passed": False, "reason": "round did not finish", "exit_code": code}
        report["passed"] = report.get("passed", False) and code == 0
        reports.append(report)
        summary = {"scope": "controlled browser adapters; real scheduler and SQLite; no live Instagram/Windows UI",
                   "requested_rounds": args.rounds, "completed_rounds": len(reports),
                   "passed": len(reports) == args.rounds and all(x["passed"] for x in reports),
                   "rounds": reports}
        (output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
        print(f"ROUND {len(reports)}/{args.rounds} {'PASS' if report['passed'] else 'FAIL'} "
              f"tests={report.get('tests', 0)} seconds={report.get('seconds', '?')}", flush=True)
        if not report["passed"]:
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
