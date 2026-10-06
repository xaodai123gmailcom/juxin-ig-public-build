from __future__ import annotations

import threading
import unittest
from unittest.mock import patch

from app.parent_watchdog import (
    PARENT_PID_ENV,
    ParentProcessWatchdog,
    start_parent_watchdog_from_env,
)


class FakeWindowsProcessApi:
    def __init__(self, *, handle: int | None = 91, results: list[int] | None = None) -> None:
        self.handle = handle
        self.results = list(results or [])
        self.opened_pid: int | None = None
        self.closed_handles: list[int] = []

    def open_process(self, pid: int) -> int | None:
        self.opened_pid = pid
        return self.handle

    def wait_for_single_object(self, handle: int, timeout_ms: int) -> int:
        if self.results:
            return self.results.pop(0)
        return 0x00000102

    def close_handle(self, handle: int) -> None:
        self.closed_handles.append(handle)


class ParentProcessWatchdogTestCase(unittest.TestCase):
    def test_unset_environment_keeps_direct_cli_runs_independent(self) -> None:
        with patch.object(ParentProcessWatchdog, "start") as start:
            self.assertIsNone(start_parent_watchdog_from_env({}))
        start.assert_not_called()

    def test_invalid_environment_is_rejected(self) -> None:
        for raw_value in ("not-a-pid", "0", "-4"):
            with self.subTest(raw_value=raw_value):
                with self.assertRaises(ValueError):
                    start_parent_watchdog_from_env({PARENT_PID_ENV: raw_value})

    def test_posix_parent_change_exits_promptly(self) -> None:
        exited = threading.Event()
        exit_codes: list[int] = []

        def exit_process(code: int) -> None:
            exit_codes.append(code)
            exited.set()

        watchdog = ParentProcessWatchdog(
            4100,
            check_interval_seconds=0.005,
            exit_process=exit_process,
            platform_name="linux",
            get_parent_pid=lambda: 9999,
        )
        watchdog.start()
        self.assertTrue(exited.wait(0.5))
        watchdog.stop()
        self.assertEqual([0], exit_codes)

    def test_windows_uses_process_handle_and_survives_timeouts(self) -> None:
        exited = threading.Event()
        exit_codes: list[int] = []
        api = FakeWindowsProcessApi(results=[0x00000102, 0x00000102, 0x00000000])

        def exit_process(code: int) -> None:
            exit_codes.append(code)
            exited.set()

        watchdog = ParentProcessWatchdog(
            5200,
            check_interval_seconds=0.001,
            exit_process=exit_process,
            platform_name="win32",
            get_parent_pid=lambda: 5200,
            windows_api=api,
        )
        watchdog.start()
        self.assertTrue(exited.wait(0.5))
        watchdog.stop()
        self.assertEqual(5200, api.opened_pid)
        self.assertEqual([91], api.closed_handles)
        self.assertEqual([0], exit_codes)

    def test_windows_open_failure_fails_closed(self) -> None:
        exit_codes: list[int] = []
        api = FakeWindowsProcessApi(handle=None)
        watchdog = ParentProcessWatchdog(
            6300,
            exit_process=exit_codes.append,
            platform_name="win32",
            get_parent_pid=lambda: 6300,
            windows_api=api,
        )

        watchdog._run()

        self.assertEqual([0], exit_codes)
        self.assertEqual([], api.closed_handles)

    def test_environment_factory_starts_configured_watchdog(self) -> None:
        with patch("app.parent_watchdog.os.getpid", return_value=7000):
            with patch.object(ParentProcessWatchdog, "start") as start:
                watchdog = start_parent_watchdog_from_env({PARENT_PID_ENV: " 7100 "})
        self.assertIsInstance(watchdog, ParentProcessWatchdog)
        self.assertEqual(7100, watchdog.parent_pid)
        start.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
