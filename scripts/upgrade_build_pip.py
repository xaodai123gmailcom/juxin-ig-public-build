#!/usr/bin/env python3
"""Best-effort pip update; only a verified local pip can continue a build."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys

# Windows invokes this entry point with -I. Load helpers only from this checked
# source directory, never an inherited PYTHONPATH or the caller's directory.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ensure_python_environment import ISOLATED, PIP_PROBE
from install_python_dependencies import OFFICIAL_INDEX, official_environment


def upgrade_pip(project, *, runner=subprocess.run, environ=None, output=print):
    project = Path(project).resolve()
    root = project / ".venv"
    inherited = dict(os.environ if environ is None else environ)
    verify = [sys.executable, *ISOLATED, "-c", PIP_PROBE, str(root)]
    base = [sys.executable, *ISOLATED, "-m", "pip", "--disable-pip-version-check",
            "--no-input", "--retries", "1", "--timeout", "15",
            "install", "--upgrade", "pip", "--prefix", str(root)]

    def run(stage, command, env, timeout):
        output("PIP_UPGRADE_STAGE=" + stage, flush=True)
        result = runner(command, cwd=project, env=env, stdin=subprocess.DEVNULL,
                        check=False, timeout=timeout)
        output("PIP_UPGRADE_EXIT=" + stage + ":" + str(result.returncode), flush=True)
        return result.returncode

    try:
        # Updating an unusable pip is environment repair's responsibility.
        if run("verify-before", verify, inherited, 30) != 0:
            output("PIP_UPGRADE_CHECK=FAILED: project pip is not usable; repair the build environment.", flush=True)
            return 1
        for attempt in ("configured", "official"):
            env = inherited if attempt == "configured" else official_environment(inherited)
            command = base if attempt == "configured" else base + ["--index-url", OFFICIAL_INDEX, "--no-cache-dir"]
            if run(attempt, command, env, 120) == 0:
                if run("verify-after", verify, env, 30) == 0:
                    output("PIP_UPGRADE_CHECK=UPDATED_AND_VERIFIED", flush=True)
                    return 0
        # A failed optional update is not a failed application dependency install.
        # Verify modules/metadata still belong to this exact venv before reuse.
        if run("verify-retained", verify, inherited, 30) == 0:
            output("PIP_UPGRADE_CHECK=KEPT_VERIFIED_LOCAL_PIP", flush=True)
            output("Pip update was unavailable. Continuing with verified local pip; pinned dependencies and pip check must still pass.", flush=True)
            return 0
    except subprocess.TimeoutExpired:
        # Do not start another writer while a timed-out installer might have
        # left a child process. The build can be retried after environment repair.
        output("PIP_UPGRADE_CHECK=FAILED: command deadline exceeded; no further installer was started.", flush=True)
        return 1
    except OSError as exc:
        output("PIP_UPGRADE_CHECK=FAILED: " + type(exc).__name__, flush=True)
        return 1
    output("PIP_UPGRADE_CHECK=FAILED: local pip did not pass verification.", flush=True)
    return 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, required=True)
    args = parser.parse_args()
    project = args.project_root.resolve()
    if sys.prefix == sys.base_prefix or Path(sys.prefix).resolve() != project / ".venv":
        print("PIP_UPGRADE_CHECK=FAILED: use this project's .venv Python; global installations are not modified.")
        return 1
    return upgrade_pip(project)


if __name__ == "__main__":
    raise SystemExit(main())
