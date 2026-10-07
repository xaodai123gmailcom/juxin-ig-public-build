#!/usr/bin/env python3
"""Cross-module runtime audits with explicit environment coverage boundaries.

The restricted-linux profile excludes real Chromium/socket and Windows-only
checks by name; original Windows release gates are not changed or bypassed.
Every round runs all remaining backend tests, frontend/desktop/source suites,
and the Python build/runtime compatibility suite. No live account is used.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import faulthandler
import importlib.util
import inspect
import json
import os
from pathlib import Path
import random
import re
import shlex
import shutil
import subprocess
import sys
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
SHARDS = 6
BROWSER_CLASSES = {
    "test_nurture_delete_ui_r41.NurtureDeleteUIR41Tests",
    "test_relation_r20.RelationDOMR20Tests",
    "test_studio_cleanup_ui.StudioCleanupUITests",
    "test_relation_dom_r51.RelationDOMR51Tests",
    "test_source_completion_r44.SourceScrollerDOMR44Tests",
    "test_zero_profile_reader_r45.ZeroProfileReaderR45Tests",
    "test_location_readiness_r45.LocationReadinessR45Tests",
    "test_relation_surface_r51.RelationSurfaceR51Tests",
    "test_zero_pipeline_r45.ZeroPipelineR45Tests",
    "test_location_pipeline_r45.LocationPipelineR45Tests",
}


def flatten(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from flatten(item)
        else:
            yield item


def inventory(seed, restricted, timings=None):
    sys.path[:0] = [str(ROOT), str(ROOT / "backend"), str(ROOT / "backend/tests")]
    all_cases = {test.id(): test for test in flatten(
        unittest.defaultTestLoader.discover(str(ROOT / "backend/tests")))}
    groups, excluded = {}, []
    for identifier, test in sorted(all_cases.items()):
        kind = identifier.rsplit(".", 1)[0]
        method = getattr(test, test._testMethodName)
        reason = None
        setup = getattr(type(test), 'asyncSetUp', None)
        setup_source = inspect.getsource(setup) if setup is not None else ''
        needs_browser = kind in BROWSER_CLASSES or any(marker in setup_source for marker in (
            'async_playwright(', '.chromium.launch(', 'launch_fixture_browser(',
        ))
        if restricted and needs_browser:
            reason = "Real Chromium cannot launch: socket creation denied by execution environment"
        elif restricted and "ThreadingHTTPServer(" in inspect.getsource(method):
            reason = "Local HTTP fixture needs socket creation, denied by execution environment"
        elif (restricted and identifier ==
              "test_openvino_windows_packaging.OpenVinoWindowsPackagingTests.test_real_cpu_models_compile_from_unicode_directory_in_memory"
              and importlib.util.find_spec("openvino") is None):
            reason = "Real OpenVINO inference requires the pinned native runtime, unavailable in this execution environment"
        elif getattr(method, "__unittest_skip__", False) or getattr(type(test), "__unittest_skip__", False):
            reason = getattr(method, "__unittest_skip_why__", "platform capability unavailable")
        if reason:
            excluded.append({"test": identifier, "reason": reason})
        else:
            groups.setdefault(kind, []).append(test)
    rng = random.Random(seed)
    classes = list(groups.values())
    rng.shuffle(classes)
    shards = [[] for _ in range(SHARDS)]
    timings = timings or {}
    def weight(group):
        return sum(timings.get(test.id(), .1) for test in group)
    # Keep class setup/teardown together, while avoiding one slow shard holding
    # every following round. Every selected test still runs exactly once.
    classes.sort(key=weight, reverse=True)
    loads = [0.0] * SHARDS
    for group in classes:
        rng.shuffle(group)
        target = min(range(SHARDS), key=lambda index: loads[index])
        shards[target].extend(group)
        loads[target] += weight(group)
    return shards, excluded


class RuntimeResult(unittest.TextTestResult):
    def startTest(self, test):
        if not hasattr(self, 'case_seconds'):
            self.case_seconds = {}
        self.case_started = time.monotonic()
        faulthandler.dump_traceback_later(180, exit=True)
        super().startTest(test)

    def stopTest(self, test):
        self.case_seconds[test.id()] = round(time.monotonic() - self.case_started, 6)
        super().stopTest(test)
        faulthandler.dump_traceback_later(180, exit=True)


def backend(args):
    timings = {}
    # Only completed previous rounds supply scheduling estimates. They never
    # change test assertions, deadlines, or which cases are selected.
    if args.report is not None:
        previous = sorted(p for p in args.report.parent.parent.glob('round-*')
                          if p.is_dir() and p.name < args.report.parent.name)
        for folder in previous[-1:]:
            for report in folder.glob('backend-*.json'):
                timings.update(json.loads(report.read_text(encoding="utf-8")).get('case_seconds', {}))
    shards, excluded = inventory(args.seed, args.profile == "restricted-linux", timings)
    chosen = shards[args.backend_shard]
    started = time.monotonic()
    result = unittest.TextTestRunner(verbosity=2, resultclass=RuntimeResult,
                                    failfast=False).run(unittest.TestSuite(chosen))
    faulthandler.cancel_dump_traceback_later()
    report = {"seed": args.seed, "shard": args.backend_shard, "tests": result.testsRun,
              "expected_tests": len(chosen), "failures": len(result.failures), "errors": len(result.errors),
              "skipped": [(test.id(), why) for test, why in result.skipped],
              "failed_tests": [test.id() for test, _ in result.failures + result.errors],
              "excluded": excluded, "test_order": [test.id() for test in chosen],
              "case_seconds": result.case_seconds,
              "seconds": round(time.monotonic() - started, 3),
              "passed": result.wasSuccessful() and result.testsRun == len(chosen)}
    args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return 0 if report["passed"] else 1


def validate_stage_evidence(stage, folder):
    """An exit code alone cannot prove a complete test run."""
    if stage.startswith("backend-"):
        report = json.loads((folder / f"{stage}.json").read_text(encoding="utf-8"))
        if (not report["passed"] or report["failures"] or report["errors"]
                or report["skipped"] or not report["tests"]
                or report["tests"] != report["expected_tests"]
                or report["tests"] != len(set(report["test_order"]))):
            raise ValueError(f"Incomplete or unsuccessful {stage} evidence")
        return report["tests"]
    text = (folder / f"{stage}.log").read_text(encoding="utf-8")
    if stage == "compatibility":
        counts = re.findall(r"^Ran (\d+) tests? in ", text, re.M)
        if not counts or int(counts[-1]) < 75 or not re.search(r"^OK\s*$", text, re.M):
            raise ValueError("Missing complete Python compatibility result")
        return int(counts[-1])
    minimum = {"renderer": 315, "desktop": 462, "source": 39}[stage]
    fields = {key: list(map(int, re.findall(rf"(?:ℹ|#) {key} (\d+)", text)))
              for key in ("tests", "pass", "fail", "cancelled", "skipped", "todo")}
    if (sum(fields["tests"]) < minimum or fields["tests"] != fields["pass"]
            or any(len(values) != len(fields["tests"]) for values in fields.values())
            or any(sum(fields[key]) for key in ("fail", "cancelled", "skipped", "todo"))):
        raise ValueError(f"Missing complete {stage} result (minimum {minimum} cases)")
    return sum(fields["tests"])


def restore_rounds(output, requested, seed_start, profile):
    """Resume only verified completed rounds; retain an interrupted round."""
    summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))
    rounds = summary["rounds"]
    if (summary["profile"] != profile or summary["requested_rounds"] != requested
            or summary["completed_rounds"] != len(rounds) or len(rounds) > requested):
        raise ValueError("Resume arguments do not match the existing audit")
    expected_stages = {f"backend-{i}" for i in range(SHARDS)} | {
        "compatibility", "renderer", "desktop", "source"}
    for number, report in enumerate(rounds, 1):
        folder = output / f"round-{number:02d}"
        stages = report["stages"]
        if (report["round"] != number or report["seed"] != seed_start + number - 1
                or not report["passed"] or len(stages) != len(expected_stages)
                or {stage["stage"] for stage in stages} != expected_stages
                or any(not stage["passed"] or stage["exit_code"] != 0 for stage in stages)):
            raise ValueError(f"Round {number} has not passed completely")
        for stage in expected_stages:
            if not (folder / f"{stage}.log").is_file():
                raise ValueError(f"Missing round {number} evidence: {stage}")
            validate_stage_evidence(stage, folder)
        identifiers = []
        for index in range(SHARDS):
            shard = json.loads((folder / f"backend-{index}.json").read_text(encoding="utf-8"))
            if (not shard["passed"] or shard["failures"] or shard["errors"]
                    or shard["tests"] != shard["expected_tests"]
                    or len(shard["test_order"]) != shard["tests"]
                    or shard["seed"] != report["seed"] or shard["shard"] != index):
                raise ValueError(f"Invalid round {number} backend evidence")
            identifiers.extend(shard["test_order"])
        if len(identifiers) != len(set(identifiers)):
            raise ValueError(f"Duplicate round {number} backend cases")
    if len(rounds) < requested:
        partial = output / f"round-{len(rounds) + 1:02d}"
        if partial.exists():
            retained = output / f"interrupted-{partial.name}-{time.time_ns()}"
            partial.rename(retained)
            print(f"Retained incomplete evidence: {retained.name}", flush=True)
    return rounds


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rounds", type=int, default=50)
    parser.add_argument("--seed-start", type=int, default=101)
    parser.add_argument("--profile", choices=("all", "restricted-linux"), default="all")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--backend-shard", type=int, choices=range(SHARDS))
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--resume", action="store_true",
                        help="Verify completed evidence and restart only the interrupted round")
    args = parser.parse_args()
    if args.backend_shard is not None:
        return backend(args)
    if args.output_dir is None or args.rounds < 1:
        parser.error("--output-dir and positive --rounds are required")
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    package = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
    desktop = shlex.split(package["scripts"]["test:desktop"].split("&&", 1)[1].strip())
    npm = shutil.which("npm.cmd" if os.name == "nt" else "npm")
    if not npm:
        parser.error("npm is required")
    rounds = (restore_rounds(output, args.rounds, args.seed_start, args.profile)
              if args.resume else [])
    if not args.resume and (output / "summary.json").exists():
        parser.error("Existing audit found; use --resume or a new output directory")
    for number in range(len(rounds) + 1, args.rounds + 1):
        seed = args.seed_start + number - 1
        print(f"SOFTWARE ROUND {number}/{args.rounds} START seed={seed}", flush=True)
        folder = output / f"round-{number:02d}"
        folder.mkdir()
        started = time.monotonic()
        env = {**os.environ, "PYTHONHASHSEED": str(seed), "R94_AUDIT_SEED": str(seed)}
        jobs = [(f"backend-{i}", [sys.executable, "-X", "utf8", str(Path(__file__).resolve()),
                 "--backend-shard", str(i), "--seed", str(seed), "--profile", args.profile,
                 "--report", str(folder / f"backend-{i}.json")]) for i in range(SHARDS)]
        jobs += [("compatibility", [sys.executable, "-I", "-X", "utf8", "-m", "unittest",
                    "discover", "-s", "scripts/tests", "-p", "test_*.py", "-v"]),
                 ("renderer", [npm, "run", "test:renderer"]),
                 ("desktop", desktop), ("source", [npm, "run", "test:source"])]

        def run(job):
            name, command = job
            start = time.monotonic()
            with (folder / f"{name}.log").open("wb") as log:
                try:
                    result = subprocess.run(command, cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
                        stdout=log, stderr=subprocess.STDOUT, timeout=1800)
                    code = result.returncode
                except subprocess.TimeoutExpired:
                    code = 124
            completed_tests = None
            evidence_error = None
            if code == 0:
                try:
                    completed_tests = validate_stage_evidence(name, folder)
                except (OSError, ValueError, KeyError, TypeError) as error:
                    code = 125
                    evidence_error = str(error)
            result = {"stage": name, "exit_code": code, "passed": code == 0,
                      "seconds": round(time.monotonic() - start, 3),
                      "completed_tests": completed_tests, "evidence_error": evidence_error}
            print(f"SOFTWARE ROUND {number} {name} {'PASS' if code == 0 else 'FAIL'}", flush=True)
            return result

        with ThreadPoolExecutor(max_workers=min(SHARDS + 1, os.cpu_count() or 1)) as pool:
            stages = list(pool.map(run, jobs))
        report = {"round": number, "seed": seed, "stages": stages,
                  "passed": all(stage["passed"] for stage in stages),
                  "seconds": round(time.monotonic() - started, 3)}
        rounds.append(report)
        pending_summary = output / "summary.pending.json"
        pending_summary.write_text(json.dumps({"profile": args.profile,
            "scope": __doc__, "requested_rounds": args.rounds, "completed_rounds": len(rounds),
            "passed": len(rounds) == args.rounds and all(r["passed"] for r in rounds),
            "rounds": rounds}, indent=2) + "\n", encoding="utf-8")
        pending_summary.replace(output / "summary.json")
        print(f"SOFTWARE ROUND {number}/{args.rounds} {'PASS' if report['passed'] else 'FAIL'} "
              f"seconds={report['seconds']}", flush=True)
        if not report["passed"]:
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
