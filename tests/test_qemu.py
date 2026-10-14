import json
import sys
import tempfile
import unittest
from pathlib import Path

from lk_ax.qemu import QemuChecks
from lk_ax.runtime import DemoAgent, Orchestrator


SUCCESS = "welcome to lk/MP\nCASES: 53 SUCCESS: 53 FAILED: 0\nSUCCESS! All test cases passed\n"


class QemuChecksTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "makefile").touch()
        self.checks = QemuChecks(self.root, test_timeout=0.1)
        self.output = self.root / "run"
        self.output.mkdir()
        self.checks.bind_output(self.output)

    def command(self, code):
        self.checks.command = lambda name: [sys.executable, "-c", code]

    def test_test_success_requires_guest_summary(self):
        self.command(f"print({SUCCESS!r})")
        result = self.checks.run("test", 1)
        self.assertTrue(result["passed"])
        self.assertFalse(result["stub"])
        self.assertEqual(result["test_cases"], {"total": 53, "passed": 53, "failed": 0})
        self.assertIn("CASES", Path(result["log_path"]).read_text())

    def test_zero_exit_without_tests_fails(self):
        self.command("print('welcome to lk/MP')")
        self.assertFalse(self.checks.run("test", 1)["passed"])

    def test_failed_cases_override_success_message(self):
        self.command(f"print({SUCCESS.replace('FAILED: 0', 'FAILED: 1')!r})")
        self.assertFalse(self.checks.run("test", 1)["passed"])

    def test_process_failure_overrides_success_message(self):
        self.command(f"print({SUCCESS!r}); raise SystemExit(1)")
        self.assertFalse(self.checks.run("test", 1)["passed"])

    def test_guest_panic_overrides_success_message(self):
        self.command(f"print({(SUCCESS + 'PANIC: unexpected exception')!r})")
        self.assertFalse(self.checks.run("test", 1)["passed"])

    def test_no_test_cases_is_not_success(self):
        self.command(f"print({SUCCESS.replace('53', '0')!r})")
        self.assertFalse(self.checks.run("test", 1)["passed"])

    def test_commands_use_isolated_build_artifact(self):
        build = self.checks.command("build")
        test = self.checks.command("test")
        self.assertIn(f"BUILDROOT={self.output / 'build'}", build)
        self.assertIn("IGNORE_LOCAL_MK=1", build)
        self.assertIn("-mno-outline-atomics", next(arg for arg in build if arg.startswith("CC=")))
        self.assertIn(str(self.checks.build_dir / "lk.elf"), test)

    def test_timeout_fails_even_after_success_marker(self):
        self.command(f"import time; print({SUCCESS!r}, flush=True); time.sleep(5)")
        result = self.checks.run("test", 1)
        self.assertTrue(result["timed_out"])
        self.assertFalse(result["passed"])
        self.assertIsNotNone(result["exit_code"])

    def test_missing_binary_is_failure(self):
        self.command("print('build finished')")
        self.assertFalse(self.checks.run("build", 1)["passed"])

    def test_missing_executable_is_recorded(self):
        self.checks.command = lambda name: [str(self.root / "missing-executable")]
        result = self.checks.run("build", 1)
        self.assertFalse(result["passed"])
        self.assertIsNone(result["exit_code"])
        self.assertIn("missing-executable", result["log_tail"])

    def test_real_backend_retry_passes_logs_to_agents(self):
        class FakeTools(QemuChecks):
            def command(self, name):
                if name == "build":
                    code = ("from pathlib import Path; "
                            f"p=Path({str(self.build_dir)!r}); p.mkdir(parents=True, exist_ok=True); "
                            "(p/'lk.elf').write_bytes(b'elf'); (p/'lk.bin').write_bytes(b'bin')")
                else:
                    marker = self.output / "first-test"
                    code = ("from pathlib import Path; "
                            f"p=Path({str(marker)!r}); first=not p.exists(); p.touch(); "
                            f"print('FAILURE! simulated guest failure' if first else {SUCCESS!r}); "
                            "raise SystemExit(1 if first else 0)")
                return [sys.executable, "-c", code]
        output = self.root / "e2e"
        checks = FakeTools(self.root)
        report = Orchestrator(DemoAgent(), checks, output).run("검증")
        self.assertEqual(report["status"], "completed")
        self.assertEqual(report["validation_mode"], "qemu")
        self.assertEqual(report["context"]["attempt"], 2)
        failure = report["context"]["implementation"]["diagnosis"]["failed_check"]
        self.assertIn("simulated guest failure", failure["log_tail"])
        events = [json.loads(x) for x in (output / "events.jsonl").read_text().splitlines()]
        self.assertEqual(sum(e["stage"] == "build" for e in events), 2)


if __name__ == "__main__":
    unittest.main()
