#!/usr/bin/env python3
"""Repeat actual desktop builds from full/overlay archives in isolated folders.

Uses an existing verified node_modules directory, not a network reinstall.
This validates source/TypeScript/Vite/desktop contracts, not Windows EXE,
PowerShell execution, native window integration, or PyInstaller packaging.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import time
import zipfile


SCENARIOS = ["clean", "overlay", "path with spaces", "聚鑫 构建 (4)",
             "stale outputs", "interrupted outputs", "cached rebuild",
             "foreign cwd", "missing artifact repair", "repeat clean"]


def extract(archive, destination):
    with zipfile.ZipFile(archive) as source:
        for name in source.namelist():
            if PurePosixPath(name).is_absolute() or ".." in PurePosixPath(name).parts or "\\" in name:
                raise ValueError("Unsafe archive path")
        source.extractall(destination)


def fingerprints(root):
    return {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for directory in (root / "renderer/dist", root / "dist-electron")
            for path in sorted(directory.rglob("*")) if path.is_file()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for option in ("full-zip", "base-zip", "overlay-zip", "dependencies", "output-dir"):
        parser.add_argument("--" + option, required=True, type=Path)
    args = parser.parse_args()
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    dependencies = args.dependencies.resolve()
    if not (dependencies / "typescript/package.json").is_file():
        parser.error("--dependencies must be the existing node_modules directory")
    node = shutil.which("node")
    npm = shutil.which("npm.cmd" if os.name == "nt" else "npm")
    if not node or not npm:
        parser.error("node and npm are required")
    reports, baseline, previous = [], None, None
    for number, scenario in enumerate(SCENARIOS, 1):
        print(f"BUILD ROUND {number}/10 START {scenario}", flush=True)
        root = output / f"case-{number:02d}-{scenario}"
        if root.exists():
            raise RuntimeError("Use a fresh output directory; existing cases are not overwritten")
        root.mkdir()
        if number % 2 == 0:
            extract(args.base_zip, root)
            extract(args.overlay_zip, root)
        else:
            extract(args.full_zip, root)
        (root / "node_modules").symlink_to(dependencies, target_is_directory=True)
        if number == 7 and previous is not None:
            for directory in ("renderer/dist", "dist-electron"):
                shutil.copytree(previous / directory, root / directory)
        if number in (5, 6):
            for relative in ("renderer/dist/assets/stale-r94.js", "dist-electron/stale-r94.js"):
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("incomplete prior build", encoding="utf-8")
        report = {"round": number, "scenario": scenario,
                  "archive_mode": "overlay" if number % 2 == 0 else "full",
                  "stages": [], "passed": False}
        started = time.monotonic()

        def run(name, command, *, expected=0):
            log_path = output / f"round-{number:02d}-{name}.log"
            start = time.monotonic()
            with log_path.open("wb") as log:
                result = subprocess.run(command, cwd=output if number == 8 else root,
                    stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, timeout=600)
            report["stages"].append({"stage": name, "exit_code": result.returncode,
                "expected_exit_code": expected, "seconds": round(time.monotonic() - start, 3),
                "log": log_path.name})
            if result.returncode != expected:
                raise RuntimeError(f"{name} failed; see {log_path.name}")

        try:
            run("source-before", [node, str(root / "scripts/verify_build_source.mjs")])
            run("dependencies", [npm, "--prefix", str(root), "ls", "--depth=0", "--silent"])
            if number == 9:
                # Missing compiled artifacts must fail before a rebuild repairs them.
                run("missing-artifact-rejected", [node, str(root / "scripts/verify_desktop_build.mjs")], expected=1)
            run("build", [npm, "--prefix", str(root), "run", "build"])
            run("pip-recovery", [sys.executable, "-I", "-X", "utf8", "-m", "unittest",
                "discover", "-s", str(root / "scripts/tests"), "-p", "test_pip_upgrade_r94.py"])
            run("python-compile", [sys.executable, "-I", "-X", "utf8", "-m", "compileall", "-q",
                str(root / "backend/app"), str(root / "scripts")])
            run("source-after", [node, str(root / "scripts/verify_build_source.mjs")])
            for relative in ("renderer/dist/assets/stale-r94.js", "dist-electron/stale-r94.js"):
                if (root / relative).exists():
                    raise RuntimeError("Stale compiled output survived the build")
            built = fingerprints(root)
            if baseline is None:
                baseline = built
            if not built or built != baseline:
                raise RuntimeError("Compiled artifacts differ between equivalent archive builds")
            report["artifact_files"] = len(built)
            report["artifacts_identical"] = True
            report["passed"] = True
        except (RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
            report["error"] = str(exc)
        report["seconds"] = round(time.monotonic() - started, 3)
        reports.append(report)
        (output / "summary.json").write_text(json.dumps({
            "scope": __doc__, "completed_rounds": len(reports),
            "passed": len(reports) == 10 and all(r["passed"] for r in reports),
            "rounds": reports, "artifact_sha256": baseline,
        }, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"BUILD ROUND {number}/10 {'PASS' if report['passed'] else 'FAIL'} "
              f"seconds={report['seconds']}", flush=True)
        if not report["passed"]:
            return 1
        previous = root
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
