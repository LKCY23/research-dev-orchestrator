import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


SOURCE_ROOT = Path(__file__).resolve().parents[2]
RUNNER_FILES = (
    "test_runner_lib.sh",
    "run_unit_tests.sh",
    "run_smoke_tests.sh",
    "run_all_tests.sh",
)


class TestRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.scripts = self.root / "scripts"
        self.unit_dir = self.root / "tests" / "unit"
        self.smoke_dir = self.root / "tests" / "smoke"
        self.scripts.mkdir()
        self.unit_dir.mkdir(parents=True)
        self.smoke_dir.mkdir(parents=True)
        for name in RUNNER_FILES:
            shutil.copy2(SOURCE_ROOT / "scripts" / name, self.scripts / name)

        self.call_log = self.root / "calls.log"
        self._run_index = 0
        self.write_unit("test_alpha.py", "unit-alpha")
        self.write_unit("test_beta.py", "unit-beta")
        self.write_smoke("test_one.sh", "smoke-one")
        self.write_smoke("test_two.sh", "smoke-two")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write_unit(
        self,
        name: str,
        marker: str,
        *,
        fail: bool = False,
        noise_lines: int = 1,
    ) -> None:
        noise = "\n".join(
            f'print("{marker.upper()}_NOISE_{index:02d}")'
            for index in range(1, noise_lines + 1)
        )
        assertion = "self.fail('fixture failure')" if fail else "self.assertTrue(True)"
        (self.unit_dir / name).write_text(
            f"""import os
import unittest

with open(os.environ["RDO_RUNNER_CALL_LOG"], "a", encoding="utf-8") as handle:
    handle.write("{marker}\\n")
{noise}

class FixtureTest(unittest.TestCase):
    def test_fixture(self):
        {assertion}
""",
            encoding="utf-8",
        )

    def write_smoke(
        self,
        name: str,
        marker: str,
        *,
        exit_code: int = 0,
        noise_lines: int = 1,
    ) -> None:
        body = [
            "#!/usr/bin/env bash",
            "set -euo pipefail",
            f"printf '%s\\n' '{marker}' >> \"${{RDO_RUNNER_CALL_LOG}}\"",
        ]
        body.extend(
            f"echo '{marker.upper()}_NOISE_{index:02d}'"
            for index in range(1, noise_lines + 1)
        )
        body.append(f"exit {exit_code}")
        path = self.smoke_dir / name
        path.write_text("\n".join(body) + "\n", encoding="utf-8")
        path.chmod(0o755)

    def run_runner(self, name: str, *arguments: str) -> tuple[subprocess.CompletedProcess[str], Path]:
        self._run_index += 1
        log_dir = self.root / "logs" / str(self._run_index)
        environment = os.environ.copy()
        environment.update(
            {
                "PYTHONDONTWRITEBYTECODE": "1",
                "RDO_RUNNER_CALL_LOG": str(self.call_log),
                "RDO_TEST_LOG_DIR": str(log_dir),
                "RDO_TEST_TAIL_LINES": "5",
            }
        )
        result = subprocess.run(
            [str(self.scripts / name), *arguments],
            cwd=self.root,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )
        return result, log_dir

    def calls(self) -> list[str]:
        if not self.call_log.exists():
            return []
        return self.call_log.read_text(encoding="utf-8").splitlines()

    def clear_calls(self) -> None:
        self.call_log.unlink(missing_ok=True)

    def test_unit_only_is_compact_and_does_not_run_smoke(self) -> None:
        result, log_dir = self.run_runner("run_unit_tests.sh")

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(["unit-alpha", "unit-beta"], self.calls())
        self.assertIn("PASS unit: Ran 2 tests", result.stdout)
        self.assertNotIn("UNIT-ALPHA_NOISE", result.stdout + result.stderr)
        self.assertIn(
            "UNIT-ALPHA_NOISE",
            (log_dir / "unit.log").read_text(encoding="utf-8"),
        )

    def test_smoke_only_is_compact_and_preserves_order(self) -> None:
        result, log_dir = self.run_runner("run_smoke_tests.sh")

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(["smoke-one", "smoke-two"], self.calls())
        self.assertIn("PASS smoke: 2 scripts", result.stdout)
        self.assertNotIn("SMOKE-ONE_NOISE", result.stdout + result.stderr)
        self.assertIn(
            "SMOKE-ONE_NOISE",
            (log_dir / "smoke-test_one.sh.log").read_text(encoding="utf-8"),
        )

    def test_all_tests_invokes_each_suite_once(self) -> None:
        result, _log_dir = self.run_runner("run_all_tests.sh")

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(
            ["unit-alpha", "unit-beta", "smoke-one", "smoke-two"],
            self.calls(),
        )
        self.assertEqual(1, result.stdout.count("PASS unit:"))
        self.assertEqual(1, result.stdout.count("PASS smoke: 2 scripts"))
        self.assertEqual(1, result.stdout.count("PASS all-tests"))

    def test_explicit_selectors_and_patterns(self) -> None:
        unit, _ = self.run_runner("run_unit_tests.sh", "--pattern", "test_a*.py")
        self.assertEqual(0, unit.returncode, unit.stderr)
        self.assertEqual(["unit-alpha"], self.calls())

        self.clear_calls()
        smoke_exact, _ = self.run_runner(
            "run_smoke_tests.sh", "--match", "test_two.sh"
        )
        self.assertEqual(0, smoke_exact.returncode, smoke_exact.stderr)
        self.assertEqual(["smoke-two"], self.calls())

        self.clear_calls()
        smoke_pattern, _ = self.run_runner(
            "run_smoke_tests.sh", "--match", "test_t*.sh"
        )
        self.assertEqual(0, smoke_pattern.returncode, smoke_pattern.stderr)
        self.assertEqual(["smoke-two"], self.calls())

    def test_zero_match_is_an_explicit_failure(self) -> None:
        unit, _ = self.run_runner("run_unit_tests.sh", "--pattern", "missing*.py")
        smoke, _ = self.run_runner("run_smoke_tests.sh", "--match", "missing*.sh")

        self.assertEqual(2, unit.returncode)
        self.assertIn("unit selector matched no tests", unit.stderr)
        self.assertEqual(2, smoke.returncode)
        self.assertIn("smoke selector matched no tests", smoke.stderr)
        self.assertEqual([], self.calls())

    def test_unit_file_that_loads_no_tests_is_an_explicit_failure(self) -> None:
        (self.unit_dir / "test_empty.py").write_text(
            "EMPTY_FIXTURE = True\n",
            encoding="utf-8",
        )

        result, log_dir = self.run_runner(
            "run_unit_tests.sh", "--pattern", "test_empty.py"
        )

        self.assertEqual(2, result.returncode)
        self.assertIn("unit selector loaded no tests", result.stderr)
        self.assertIn("Ran 0 tests", (log_dir / "unit.log").read_text(encoding="utf-8"))

    def test_missing_selector_value_is_a_usage_failure(self) -> None:
        unit, _ = self.run_runner("run_unit_tests.sh", "--pattern")
        smoke, _ = self.run_runner("run_smoke_tests.sh", "--match")

        self.assertEqual(2, unit.returncode)
        self.assertIn("usage: run_unit_tests.sh", unit.stderr)
        self.assertNotIn("unbound variable", unit.stderr)
        self.assertEqual(2, smoke.returncode)
        self.assertIn("usage: run_smoke_tests.sh", smoke.stderr)
        self.assertNotIn("unbound variable", smoke.stderr)

    def test_smoke_failure_preserves_exit_and_bounds_diagnostic(self) -> None:
        self.write_smoke(
            "test_fail.sh",
            "smoke-fail",
            exit_code=7,
            noise_lines=20,
        )

        result, log_dir = self.run_runner(
            "run_smoke_tests.sh", "--match", "test_fail.sh"
        )

        self.assertEqual(7, result.returncode)
        self.assertNotIn("SMOKE-FAIL_NOISE_01", result.stderr)
        self.assertIn("SMOKE-FAIL_NOISE_20", result.stderr)
        self.assertLessEqual(len(result.stderr.splitlines()), 8)
        full_log = (log_dir / "smoke-test_fail.sh.log").read_text(encoding="utf-8")
        self.assertIn("SMOKE-FAIL_NOISE_01", full_log)
        self.assertIn("SMOKE-FAIL_NOISE_20", full_log)

    def test_unit_failure_is_bounded_and_all_tests_fail_fast(self) -> None:
        self.write_unit(
            "test_aaa_fail.py",
            "unit-fail",
            fail=True,
            noise_lines=20,
        )

        unit, log_dir = self.run_runner(
            "run_unit_tests.sh", "--pattern", "test_aaa_fail.py"
        )
        self.assertEqual(1, unit.returncode)
        self.assertNotIn("UNIT-FAIL_NOISE_01", unit.stderr)
        self.assertIn(
            "UNIT-FAIL_NOISE_01",
            (log_dir / "unit.log").read_text(encoding="utf-8"),
        )

        self.clear_calls()
        all_tests, _ = self.run_runner("run_all_tests.sh")
        self.assertEqual(1, all_tests.returncode)
        self.assertIn("unit-fail", self.calls())
        self.assertNotIn("smoke-one", self.calls())
        self.assertNotIn("smoke-two", self.calls())


if __name__ == "__main__":
    unittest.main()
