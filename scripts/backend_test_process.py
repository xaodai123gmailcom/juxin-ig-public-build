"""Observe an owned backend-test process without buffering its Windows output."""
from __future__ import annotations

import codecs
import math
import os
import subprocess
import sys
import time
from pathlib import Path

from scripts.verify_frozen_core_service import direct_child_command, probe_directory


CASE_TIMEOUT_SECONDS = 180.0
PROCESS_TIMEOUT_SECONDS = 1800.0


def run_backend_process(runner, arguments, *, cwd, stream=None,
                        case_timeout=CASE_TIMEOUT_SECONDS,
                        overall_timeout=PROCESS_TIMEOUT_SECONDS):
    for name, value in (("case_timeout", case_timeout), ("overall_timeout", overall_timeout)):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise ValueError(f"{name} must be finite and positive")
    if case_timeout > overall_timeout:
        raise ValueError("case_timeout cannot exceed overall_timeout")
    environment = {key: value for key, value in os.environ.items()
                   if key.upper() not in {"PYTHONPATH", "PYTHONHOME", "__PYVENV_LAUNCHER__"}}
    command = [sys.executable, "-I", "-u", "-X", "utf8", str(runner),
               *arguments, "--case-timeout", str(case_timeout)]
    command = direct_child_command(command, environment)
    started = time.monotonic()
    last_notice = started
    captured = []
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    with probe_directory(prefix="juxin-owned-test-") as temporary:
        output_path = Path(temporary) / "process.log"
        with output_path.open("wb") as writer, output_path.open("rb") as reader:
            process = subprocess.Popen(command, cwd=cwd, env=environment,
                                       stdout=writer, stderr=subprocess.STDOUT)

            def drain(final=False):
                text = decoder.decode(reader.read(), final=final)
                captured.append(text)
                if stream is not None and text:
                    stream.write(text)
                    stream.flush()

            try:
                while True:
                    drain()
                    returncode = process.poll()
                    if returncode is not None:
                        process.wait()
                        drain(final=True)
                        return subprocess.CompletedProcess(command, returncode, "".join(captured))
                    now = time.monotonic()
                    if now - started >= overall_timeout:
                        process.kill()
                        process.wait()
                        drain(final=True)
                        output = "".join(captured)
                        raise AssertionError(
                            f"Backend test process exceeded overall limit {overall_timeout:g}s; "
                            f"owned process {process.pid} was stopped and joined.\n"
                            f"Captured output (last 16000 characters):\n{output[-16000:]}"
                        )
                    if stream is not None and now - last_notice >= 15:
                        stream.write(f"\nWAIT backend process: {now - started:.1f}s / "
                                     f"{overall_timeout:g}s; individual test limit {case_timeout:g}s\n")
                        stream.flush()
                        last_notice = now
                    time.sleep(min(.1, max(0.0, overall_timeout - (now - started))))
            finally:
                # Timeout, assertion, cancellation or output failure must never
                # leave this test's process holding SQLite/temp-file handles.
                if process.poll() is None:
                    process.kill()
                process.wait()
