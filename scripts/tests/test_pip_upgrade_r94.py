"""Offline failures and real local pip health checks for the build update step."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import venv
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import upgrade_build_pip as subject


class PipUpgradeR94Tests(unittest.TestCase):
    def sequence(self, codes, env=None):
        self.calls, self.logs = [], []
        outcomes = iter(codes)

        def runner(command, **kwargs):
            self.calls.append((command, kwargs))
            result = next(outcomes)
            if isinstance(result, BaseException):
                raise result
            return subprocess.CompletedProcess(command, result)

        with tempfile.TemporaryDirectory(prefix="Juxin 构建 (94) ") as folder:
            result = subject.upgrade_pip(folder, runner=runner, environ=env or {},
                output=lambda text, **_: self.logs.append(text))
        return result

    def test_success_is_verified_in_a_fresh_process(self):
        self.assertEqual(0, self.sequence([0, 0, 0]))
        self.assertEqual(3, len(self.calls))
        self.assertIn("verify-after:0", "\n".join(self.logs))

    def test_unavailable_update_keeps_only_verified_local_pip(self):
        self.assertEqual(0, self.sequence([0, 1, 1, 0]))
        self.assertIn("PIP_UPGRADE_CHECK=KEPT_VERIFIED_LOCAL_PIP", self.logs)
        self.assertIn(subject.PIP_PROBE, self.calls[-1][0])

    def test_stale_mirror_retries_with_clean_configuration_and_same_venv(self):
        env = {"PIP_INDEX_URL": "https://user:secret@stale.invalid/simple",
               "PIP_NO_INDEX": "1", "PIP_TARGET": "elsewhere",
               "HTTPS_PROXY": "http://proxy.invalid", "REQUESTS_CA_BUNDLE": "ca.pem"}
        self.assertEqual(0, self.sequence([0, 1, 0, 0], env))
        fallback, options = self.calls[2]
        self.assertEqual(subject.official_environment(env), options["env"])
        self.assertEqual(env, self.calls[1][1]["env"])
        self.assertEqual(self.calls[1][0][-1], fallback[fallback.index("--prefix") + 1])
        self.assertEqual(["--index-url", subject.OFFICIAL_INDEX, "--no-cache-dir"], fallback[-3:])
        self.assertNotIn("secret", "\n".join(self.logs))

    def test_broken_pip_never_turns_update_failure_into_success(self):
        self.assertEqual(1, self.sequence([0, 1, 1, 1]))
        self.assertNotIn("PIP_UPGRADE_CHECK=KEPT_VERIFIED_LOCAL_PIP", self.logs)
        self.assertEqual(1, self.sequence([1]))
        self.assertEqual(1, len(self.calls))

    def test_zero_exit_but_broken_install_is_repaired_or_fails(self):
        self.assertEqual(0, self.sequence([0, 0, 1, 0, 0]))
        self.assertEqual(5, len(self.calls))
        self.assertEqual(1, self.sequence([0, 0, 1, 0, 1, 1]))

    def test_total_timeout_never_starts_another_installer(self):
        self.assertEqual(1, self.sequence([0, subprocess.TimeoutExpired("pip", 120)]))
        self.assertEqual(2, len(self.calls))
        self.assertEqual(120, self.calls[1][1]["timeout"])

    def test_cancel_is_not_retried(self):
        with self.assertRaises(KeyboardInterrupt):
            self.sequence([0, KeyboardInterrupt()])
        self.assertEqual(2, len(self.calls))

    def test_global_python_cannot_be_modified(self):
        with tempfile.TemporaryDirectory() as folder:
            result = subprocess.run([sys.executable, *subject.ISOLATED, subject.__file__, "--project-root", folder],
                                    capture_output=True, encoding="utf-8", timeout=30)
        self.assertNotEqual(0, result.returncode)
        self.assertIn("global installations are not modified", result.stdout)

    def test_windows_entry_keeps_dependency_gate_after_verified_upgrade(self):
        root = Path(__file__).resolve().parents[2]
        text = (root / "scripts/install_windows.ps1").read_text(encoding="utf-8-sig")
        self.assertIn('scripts\\upgrade_build_pip.py', text)
        self.assertLess(text.index('scripts\\upgrade_build_pip.py'), text.index('scripts\\install_python_dependencies.py'))
        self.assertIn('if ($PythonDependenciesExitCode -ne 0)', text)


class RealPipUpgradeR94Tests(unittest.TestCase):
    def test_real_pip_paths_with_legacy_windows_pipe_encodings(self):
        # Exercise real child processes and real pip files. Linux's UTF-8
        # locale alone cannot expose a Windows CP936 pipe mismatch.
        original_popen = subprocess.Popen

        def legacy_pipe(command, *args, **kwargs):
            command = list(command)
            if "-c" in command and "pip.__file__" in command[command.index("-c") + 1]:
                explicit_utf8 = any(command[i:i + 2] == ["-X", "utf8"]
                                    for i in range(len(command) - 1))
                if not explicit_utf8:
                    command[1:1] = ["-X", "utf8=0"]
                index = command.index("-c") + 1
                command[index] = ("import sys;sys.stdout.reconfigure(encoding="
                                  "'utf-8' if sys.flags.utf8_mode else 'cp936');" + command[index])
            return original_popen(command, *args, **kwargs)

        for parent_encoding in ("utf-8", "cp936"):
            with self.subTest(parent_encoding=parent_encoding):
                with patch("subprocess.Popen", side_effect=legacy_pipe), \
                        patch("subprocess._text_encoding", return_value=parent_encoding):
                    self.test_real_local_pip_survives_offline_update_and_corruption_still_fails()

    def test_real_local_pip_survives_offline_update_and_corruption_still_fails(self):
        with tempfile.TemporaryDirectory(prefix="Juxin pip 离线 (94) ") as folder:
            root = Path(folder)
            venv.EnvBuilder(with_pip=True).create(root / ".venv")
            python = root / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            driver = root / "probe.py"
            driver.write_text('''import pathlib,subprocess,sys
sys.path.insert(0, sys.argv[2])
from upgrade_build_pip import upgrade_pip
def runner(command, **kwargs):
    if "--upgrade" in command:
        return subprocess.CompletedProcess(command, 1)
    return subprocess.run(command, **kwargs)
raise SystemExit(upgrade_pip(pathlib.Path(sys.argv[1]),runner=runner))
''', encoding="utf-8")
            args = [str(python), *subject.ISOLATED, str(driver), str(root), str(Path(subject.__file__).parent)]
            result = subprocess.run(args, capture_output=True, encoding="utf-8", timeout=30)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertIn("KEPT_VERIFIED_LOCAL_PIP", result.stdout)
            pip_path = Path(subprocess.check_output(
                [str(python), *subject.ISOLATED, "-c", "import pathlib,pip;print(pathlib.Path(pip.__file__))"],
                encoding="utf-8", timeout=30).strip())
            self.assertTrue(pip_path.resolve().is_relative_to((root / ".venv").resolve()))
            pip_path.write_text("raise ImportError('simulated damaged pip')\n", encoding="utf-8")
            damaged = subprocess.run(args, capture_output=True, encoding="utf-8", timeout=30)
            self.assertNotEqual(0, damaged.returncode)
            self.assertNotIn("KEPT_VERIFIED_LOCAL_PIP", damaged.stdout)


if __name__ == "__main__":
    unittest.main()
