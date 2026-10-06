"""Exercise the real backend test entry point in fresh, isolated Python processes."""
from __future__ import annotations

import ast
from collections import Counter
import contextlib
import fnmatch
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[2]
RUNNER = PROJECT_ROOT / "scripts" / "run_backend_tests.py"
sys.path.insert(0, str(PROJECT_ROOT))
from scripts.backend_test_process import run_backend_process
from scripts import backend_test_process, run_backend_tests


class BackendTestRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="聚鑫测试入口 (28) ")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.unrelated_cwd = self.root / "其他工作目录 (独立)"
        self.unrelated_cwd.mkdir()
        # These must never substitute for the selected checkout's imports.
        for name in ("app.py", "project_marker.py", "fixture_helper.py"):
            (self.unrelated_cwd / name).write_text(
                'raise RuntimeError("WRONG_WORKING_DIRECTORY_IMPORTED")\n',
                encoding="utf-8",
            )

    def copied_runner(self, tests: dict[str, str]) -> Path:
        project = self.root / "构建源码 空格 (3)"
        scripts = project / "scripts"
        app = project / "backend" / "app"
        test_root = project / "backend" / "tests"
        for directory in (scripts, app, test_root):
            directory.mkdir(parents=True)
        runner = scripts / RUNNER.name
        shutil.copyfile(RUNNER, runner)
        (app / "__init__.py").write_text("", encoding="utf-8")
        (app / "marker.py").write_text('VALUE = "selected backend"\n', encoding="utf-8")
        (project / "project_marker.py").write_text(
            'VALUE = "selected project"\n', encoding="utf-8"
        )
        (test_root / "fixture_helper.py").write_text(
            'VALUE = "selected tests"\n', encoding="utf-8"
        )
        for name, source in tests.items():
            (test_root / name).write_text(textwrap.dedent(source), encoding="utf-8")
        return runner

    def execute(self, runner: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
        return run_backend_process(runner, arguments, cwd=self.unrelated_cwd,
                                   stream=sys.stderr if runner == RUNNER else None)

    def assert_success(self, result: subprocess.CompletedProcess[str], count: int):
        self.assertEqual(0, result.returncode, result.stdout)
        self.assertRegex(result.stdout, rf"Ran {count} tests? in ")
        self.assertNotIn("WRONG_WORKING_DIRECTORY_IMPORTED", result.stdout)

    def test_real_target_control_suite_from_unrelated_unicode_directory(self):
        result = self.execute(RUNNER, "--pattern", "test_task_control_r27.py", "-v")
        source = PROJECT_ROOT / "backend" / "tests" / "test_task_control_r27.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))
        expected = {
            f"test_task_control_r27.{group.name}.{method.name}"
            for group in tree.body if isinstance(group, ast.ClassDef)
            for method in group.body
            if isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef))
            and method.name.startswith("test_")
        }
        self.assertTrue(expected, "the selected regression module must contain tests")
        self.assert_success(result, len(expected))
        executed = re.findall(r"(?m)^test_\w+ \(([^)]+)\) ", result.stdout)
        self.assertEqual(expected, set(executed))
        self.assertEqual(len(expected), len(executed))

    def test_pattern_selects_only_requested_suite_and_bootstraps_all_import_roots(self):
        runner = self.copied_runner({
            "test_selected.py": """
                import unittest
                from app.marker import VALUE as backend
                from project_marker import VALUE as project
                from fixture_helper import VALUE as fixture

                class ImportRoots(unittest.TestCase):
                    def test_selected_checkout(self):
                        self.assertEqual((backend, project, fixture),
                            ("selected backend", "selected project", "selected tests"))
            """,
            "test_unselected.py": 'raise RuntimeError("UNSELECTED_SUITE_IMPORTED")\n',
        })
        result = self.execute(runner, "-p", "test_selected.py", "-v")
        self.assert_success(result, 1)
        self.assertNotIn("UNSELECTED_SUITE_IMPORTED", result.stdout)

    def assert_chat_build_coverage(self, build_script):
        # R34 has its own gate; R94 fault controls run in the cumulative R94
        # gate. Check actual selected files across gates, not an obsolete
        # spelling that would force the R94 tests to execute twice.
        modules = {path.name for path in run_backend_tests.TEST_ROOT.glob('test_chat_concurrency_*.py')}
        self.assertTrue({'test_chat_concurrency_r34.py', 'test_chat_concurrency_r94.py'}.issubset(modules))
        patterns = re.findall(r'"scripts\\run_backend_tests\.py",\s*"-p",\s*"([^"]+)"', build_script)
        scheduled = Counter(name for pattern in patterns for name in fnmatch.filter(modules, pattern))
        self.assertEqual({name: 1 for name in modules}, dict(scheduled),
                         'Each chat regression module must execute exactly once across build gates')

    def test_real_chat_build_gate_includes_slow_disk_and_real_lock_controls(self):
        build_script = (PROJECT_ROOT / 'scripts' / 'build_windows.ps1').read_text(encoding='utf-8')
        self.assert_chat_build_coverage(build_script)
        result = self.execute(RUNNER, '-p', 'test_chat_concurrency_*.py', '-v')
        self.assert_success(result, 11)
        for test in ('test_chat_gate_accepts_slow_full_durability_result_and_heartbeat',
                     'test_inventory_gate_accepts_slow_full_durability_writer',
                     'test_chat_gate_rejects_real_process_writer_lock_and_cleans_up',
                     'test_chat_gate_rejects_independent_sqlite_transaction_and_cleans_up',
                     'test_inventory_gate_rejects_real_writer_lock_and_cleans_up'):
            self.assertIn(test, result.stdout)

    def test_chat_build_coverage_rejects_missing_or_duplicate_groups(self):
        source = (PROJECT_ROOT / 'scripts' / 'build_windows.ps1').read_text(encoding='utf-8')
        for old, new in (
            ('"test_*r94.py"', '"test_snapshot_maintenance_r94.py"'),
            ('"test_chat_concurrency_r34.py"', '"test_missing_chat.py"'),
            ('"test_chat_concurrency_r34.py"', '"test_chat_concurrency_*.py"'),
        ):
            with self.subTest(removed_or_changed=old, replacement=new):
                self.assertEqual(1, source.count(old))
                with self.assertRaisesRegex(AssertionError, 'Each chat regression module'):
                    self.assert_chat_build_coverage(source.replace(old, new))

    def test_installer_r94_pattern_covers_each_cumulative_regression_module(self):
        build_script = (PROJECT_ROOT / 'scripts' / 'build_windows.ps1').read_text(encoding='utf-8')
        block = build_script.split('$CollectionR94ExitCode =', 1)[1].split('if ($CollectionR94ExitCode', 1)[0]
        pattern = re.search(r'"-p",\s*"([^"]+)"', block).group(1)
        modules = {path.name for path in run_backend_tests.TEST_ROOT.glob('test_*r94.py')}
        self.assertTrue({'test_checkpoint_fence_r94.py', 'test_result_fence_r94.py',
                         'test_task_retirement_r94.py', 'test_shared_retirement_r94.py',
                         'test_retirement_concurrency_r94.py',
                         'test_recovery_cleanup_r94.py', 'test_screening_lifecycle_r94.py',
                         'test_chat_concurrency_r94.py'}.issubset(modules))
        self.assertEqual(modules, set(fnmatch.filter(modules, pattern)))

    def test_default_pattern_still_discovers_all_test_modules(self):
        runner = self.copied_runner({
            "test_first.py": """
                import unittest
                class First(unittest.TestCase):
                    def test_first(self): self.assertTrue(True)
            """,
            "test_second.py": """
                import unittest
                class Second(unittest.TestCase):
                    def test_second(self): self.assertTrue(True)
            """,
        })
        self.assert_success(self.execute(runner), 2)

    def test_import_failure_is_not_reported_as_success(self):
        runner = self.copied_runner({
            "test_broken_import.py": "import unavailable_backend_dependency_r28\n",
        })
        result = self.execute(runner, "--pattern", "test_broken_import.py")
        self.assertNotEqual(0, result.returncode, result.stdout)
        self.assertIn("unavailable_backend_dependency_r28", result.stdout)
        self.assertIn("FAILED", result.stdout)

    def test_assertion_failure_is_not_reported_as_success(self):
        runner = self.copied_runner({
            "test_failure.py": """
                import unittest
                class Failing(unittest.TestCase):
                    def test_failure(self): self.fail("INTENTIONAL_ASSERTION_R28")
            """,
        })
        result = self.execute(runner, "--pattern", "test_failure.py")
        self.assertNotEqual(0, result.returncode, result.stdout)
        self.assertIn("INTENTIONAL_ASSERTION_R28", result.stdout)
        self.assertIn("FAILED", result.stdout)

    def test_unmatched_pattern_and_empty_suite_are_errors(self):
        runner = self.copied_runner({"test_empty.py": "# No test cases.\n"})
        for pattern in ("test_missing.py", "test_empty.py"):
            with self.subTest(pattern=pattern):
                result = self.execute(runner, "--pattern", pattern)
                self.assertNotEqual(0, result.returncode, result.stdout)
                self.assertNotRegex(result.stdout, r"(?m)^OK\s*$")

    def shard_runner(self) -> Path:
        cases = "\n".join(
            f"    def test_{index:02d}(self): self.assertEqual({index}, {index})"
            for index in range(5)
        )
        return self.copied_runner({
            "test_shards.py": "import unittest\nclass ShardCases(unittest.TestCase):\n" + cases + "\n",
        })

    def test_shards_preserve_each_discovered_test_exactly_once(self):
        runner = self.shard_runner()
        executed = []
        for index, expected_count in ((0, 3), (1, 2)):
            result = self.execute(
                runner, "--pattern", "test_shards.py",
                "--shard-index", str(index), "--shard-count", "2",
            )
            self.assert_success(result, expected_count)
            executed.extend(re.findall(r"(?m)^(test_\d+) \(", result.stdout))
        self.assertEqual([f"test_{index:02d}" for index in range(5)], sorted(executed))
        self.assertEqual(5, len(set(executed)))

    def test_invalid_shard_arguments_remain_errors(self):
        runner = self.shard_runner()
        for arguments in (
            ("--shard-count", "2"),
            ("--shard-index", "-1", "--shard-count", "2"),
            ("--shard-index", "2", "--shard-count", "2"),
            ("--shard-index", "0", "--shard-count", "0"),
            ("--shard-index", "0", "--shard-count", "6"),
        ):
            with self.subTest(arguments=arguments):
                result = self.execute(runner, *arguments)
                self.assertNotEqual(0, result.returncode, result.stdout)

    def test_budget_restarts_for_each_case_and_cleanup_not_log_output(self):
        output = io.StringIO()
        result = run_backend_tests.TimedTestResult(unittest.runner._WritelnDecorator(output), True, 2)
        case = unittest.FunctionTestCase(lambda: None)
        with patch.object(run_backend_tests.faulthandler, "dump_traceback_later") as watchdog, \
                patch.object(run_backend_tests.time, "monotonic", side_effect=[0, 100, 101, 201]), \
                contextlib.redirect_stdout(output):
            result.startTest(case)
            print("heartbeat does not extend the test budget")
            self.assertEqual(1, watchdog.call_count)
            result.stopTest(case)
            result.startTest(case)
            result.stopTest(case)
        self.assertEqual(4, watchdog.call_count)
        for call in watchdog.call_args_list:
            self.assertEqual(((180.0,), {"exit": True}), (call.args, call.kwargs))
        self.assertIn("CHECK backend case 2:", output.getvalue())
        self.assertEqual(2, result.testsRun)

    def test_release_environment_sets_watchdog_without_changing_local_default(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(sys, "argv", ["runner"]):
            self.assertIsNone(run_backend_tests._parse_args().case_timeout)
        with patch.dict(os.environ, {"IGAC_TEST_CASE_TIMEOUT_SECONDS": "180"}), \
                patch.object(sys, "argv", ["runner"]):
            self.assertEqual(180, run_backend_tests._parse_args().case_timeout)
        with patch.dict(os.environ, {"IGAC_TEST_CASE_TIMEOUT_SECONDS": "180"}), \
                patch.object(sys, "argv", ["runner", "--case-timeout", "90"]):
            self.assertEqual(90, run_backend_tests._parse_args().case_timeout)
        for value in ("0", "-1", "nan", "inf", "invalid"):
            with self.subTest(value=value), patch.dict(os.environ, {"IGAC_TEST_CASE_TIMEOUT_SECONDS": value}), \
                    patch.object(sys, "argv", ["runner"]), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    run_backend_tests._parse_args()
                self.assertEqual(2, error.exception.code)

    def test_environment_watchdog_terminates_blocked_cleanup_with_failure(self):
        runner = self.copied_runner({"test_blocked_cleanup.py": """
            import threading, unittest
            class BlockedCleanup(unittest.TestCase):
                def test_body(self):
                    print('BODY_FINISHED_CLEANUP_STILL_REQUIRED', flush=True)
                def tearDown(self):
                    threading.Event().wait()
        """})
        env = os.environ.copy()
        env["IGAC_TEST_CASE_TIMEOUT_SECONDS"] = ".2"
        result = subprocess.run([sys.executable, str(runner)], cwd=self.unrelated_cwd,
                                env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, timeout=5)
        self.assertNotEqual(0, result.returncode, result.stdout)
        self.assertIn('BODY_FINISHED_CLEANUP_STILL_REQUIRED', result.stdout)
        self.assertIn('Timeout', result.stdout)
        self.assertIn('test_blocked_cleanup.py', result.stdout)
        self.assertNotRegex(result.stdout, r'(?m)^OK\s*$')

    def test_windows_full_build_enables_all_backend_case_watchdogs(self):
        script = (PROJECT_ROOT / 'scripts/build_windows.ps1').read_text(encoding='utf-8-sig')
        self.assertIn('$env:IGAC_TEST_CASE_TIMEOUT_SECONDS = "180"', script)
        self.assertIn('test_final_seed_browser_r62.py" --case-timeout 90', script)

    def test_blocked_child_emits_stack_and_failure_instead_of_losing_output(self):
        runner = self.copied_runner({"test_blocked.py": """
            import faulthandler, threading, unittest
            class Blocked(unittest.TestCase):
                def test_blocked(self):
                    print('ENTERED_CONTROLLED_BLOCK', flush=True)
                    # Arm after startup, so machine startup speed is irrelevant.
                    faulthandler.dump_traceback_later(.1, exit=True)
                    threading.Event().wait()
        """})
        result = self.execute(runner)
        self.assertNotEqual(0, result.returncode)
        self.assertIn("ENTERED_CONTROLLED_BLOCK", result.stdout)
        self.assertIn("test_blocked.py", result.stdout)
        self.assertIn("Timeout", result.stdout)
        self.assertNotRegex(result.stdout, r"(?m)^OK\s*$")

    def test_parent_deadline_joins_its_child_and_preserves_output(self):
        marker = self.root / "child-entered"
        runner = self.root / "blocked_child.py"
        runner.write_text(
            "import pathlib, threading\n"
            "print('OWNED_CHILD_OUTPUT', flush=True)\n"
            f"pathlib.Path({str(marker)!r}).touch()\n"
            "threading.Event().wait()\n", encoding="utf-8")
        spawned = []
        original = subprocess.Popen
        def record(*args, **kwargs):
            child = original(*args, **kwargs)
            spawned.append(child)
            return child
        # Expire only after this child actually starts, independent of PC speed.
        with patch.object(backend_test_process.time, "monotonic",
                          side_effect=lambda: 1000 if marker.exists() else 0), \
                patch.object(backend_test_process.subprocess, "Popen", side_effect=record):
            with self.assertRaisesRegex(AssertionError, "OWNED_CHILD_OUTPUT"):
                run_backend_process(runner, [], cwd=self.unrelated_cwd,
                                    case_timeout=50, overall_timeout=100)
        self.assertEqual(1, len(spawned))
        self.assertIsNotNone(spawned[0].poll())

    def test_invalid_deadlines_do_not_launch_a_process(self):
        with patch.object(backend_test_process.subprocess, "Popen") as spawn:
            for budget in (0, -1, float('nan'), float('inf'), True):
                with self.subTest(budget=budget), self.assertRaises(ValueError):
                    run_backend_process(RUNNER, [], cwd=self.unrelated_cwd, case_timeout=budget)
            with self.assertRaises(ValueError):
                run_backend_process(RUNNER, [], cwd=self.unrelated_cwd,
                                    case_timeout=100, overall_timeout=50)
            spawn.assert_not_called()


if __name__ == "__main__":
    unittest.main()
