import json
import sys
import tempfile
import unittest
from pathlib import Path

from lk_ax.runtime import CommandAgent, DemoAgent, Orchestrator, StubChecks


class RuntimeTests(unittest.TestCase):
    def run_case(self, scenario, agents=None, attempts=3):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "run"
            report = Orchestrator(agents or DemoAgent(), StubChecks(scenario), output, attempts).run("UART 수정")
            events = [json.loads(line) for line in (output / "events.jsonl").read_text().splitlines()]
            self.assertEqual(report, json.loads((output / "report.json").read_text()))
            return report, events

    def test_success(self):
        report, events = self.run_case("success")
        self.assertEqual(report["status"], "completed")
        self.assertEqual([e["stage"] for e in events], ["analyst", "implementer", "build", "test", "reviewer", "completed"])

    def test_failed_build_skips_test_and_retries(self):
        report, events = self.run_case("build-fails-once")
        self.assertEqual(report["status"], "completed")
        self.assertFalse(any(e["stage"] == "test" and e["attempt"] == 1 for e in events))
        self.assertEqual(report["context"]["attempt"], 2)
        self.assertIn("diagnosis", report["context"]["implementation"])

    def test_failed_test_retries(self):
        report, events = self.run_case("test-fails-once")
        self.assertEqual(report["status"], "completed")
        self.assertEqual(report["context"]["diagnosis"]["failed_check"]["name"], "test")

    def test_failure_limit(self):
        report, events = self.run_case("always-fail", attempts=2)
        self.assertEqual(report["status"], "failed")
        self.assertEqual(sum(e["stage"] == "implementer" for e in events), 2)
        self.assertFalse(any(e["stage"] == "reviewer" for e in events))

    def test_review_rejection_retries(self):
        class RejectOnce(DemoAgent):
            def run(self, role, context):
                if role == "reviewer" and context["attempt"] == 1:
                    return {"summary": "수정 필요", "approved": False}
                return super().run(role, context)
        report, _ = self.run_case("success", RejectOnce())
        self.assertEqual(report["context"]["attempt"], 2)
        self.assertEqual(report["status"], "completed")

    def test_command_contract(self):
        command = [sys.executable, "-c", "import json,sys; x=json.load(sys.stdin); print(json.dumps({'summary':x['role'],'approved':True}))"]
        agent = CommandAgent(dict.fromkeys(("analyst", "implementer", "diagnostician", "reviewer"), command), 5)
        report, _ = self.run_case("success", agent)
        self.assertEqual(report["status"], "completed")

    def test_agent_error_is_recorded(self):
        class Broken(DemoAgent):
            def run(self, role, context):
                raise ValueError("invalid response")
        report, events = self.run_case("success", Broken())
        self.assertEqual(report["status"], "failed")
        self.assertIn("invalid response", report["context"]["error"])
        self.assertEqual(events[-2]["stage"], "error")


if __name__ == "__main__":
    unittest.main()
