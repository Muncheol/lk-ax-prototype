import argparse
import json
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path


ROLES = ("analyst", "implementer", "diagnostician", "reviewer")


class DemoAgent:
    """Deterministic agent for exercising orchestration without an LLM."""

    def run(self, role, context):
        print(f"[{role}] 시작 (시도 {context['attempt']})", flush=True)
        result = self._run(role, context)
        print(f"[{role}] {result['summary']}", flush=True)
        return result

    def _run(self, role, context):
        task = context["task"]
        if role == "analyst":
            return {"summary": f"LK 변경 요청: {task}", "criteria": ["빌드 성공", "보드 테스트 성공"]}
        if role == "implementer":
            return {"summary": f"변경안 작성 (시도 {context['attempt']})", "proposal": task,
                    "diagnosis": context.get("diagnosis"), "artifact_kind": "demo_proposal"}
        if role == "diagnostician":
            return {"summary": "실패한 검증 단계 분석", "failed_check": context["failure"],
                    "next_action": "실패 로그를 반영하여 변경안 수정"}
        return {"summary": "변경안 및 검증 결과 검토", "approved": True}


class CommandAgent:
    """Run user-configured agents through a JSON stdin/stdout contract."""

    def __init__(self, commands, timeout):
        for role in ROLES:
            command = commands.get(role)
            if not isinstance(command, list) or not command or not all(isinstance(x, str) for x in command):
                raise ValueError(f"{role}: command must be a nonempty array of strings")
        self.commands = commands
        self.timeout = timeout

    def run(self, role, context):
        print(f"[{role}] 외부 에이전트 시작 (시도 {context['attempt']})", flush=True)
        process = subprocess.run(
            self.commands[role], input=json.dumps({"role": role, "context": context}, ensure_ascii=False),
            text=True, capture_output=True, timeout=self.timeout, check=True,
        )
        if process.stderr:
            print(process.stderr, file=sys.stderr, end="", flush=True)
        result = json.loads(process.stdout)
        if not isinstance(result, dict) or not isinstance(result.get("summary"), str):
            raise ValueError(f"{role}: response requires a string summary")
        if role == "reviewer" and not isinstance(result.get("approved"), bool):
            raise ValueError("reviewer: response requires a boolean approved")
        print(f"[{role}] {result['summary']}", flush=True)
        return result


class StubChecks:
    mode = "stub"

    def __init__(self, scenario):
        self.scenario = scenario

    def run(self, name, attempt):
        failed = ((self.scenario == "build-fails-once" and name == "build" and attempt == 1)
                  or (self.scenario == "test-fails-once" and name == "test" and attempt == 1)
                  or (self.scenario == "always-fail" and name == "build"))
        return {"name": name, "stub": True, "passed": not failed,
                "log": f"[STUB] {name}: {'simulated failure' if failed else 'simulated success'}"}


class Orchestrator:
    def __init__(self, agents, checks, output, max_attempts=3):
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        self.agents, self.checks = agents, checks
        self.output = Path(output)
        self.output.mkdir(parents=True, exist_ok=False)
        if hasattr(checks, "bind_output"):
            checks.bind_output(self.output)
        self.max_attempts = max_attempts
        self.context = {}

    def event(self, stage, result):
        event = {"time": datetime.now(timezone.utc).isoformat(), "stage": stage,
                 "attempt": self.context.get("attempt", 0), "result": result}
        with (self.output / "events.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event, ensure_ascii=False) + "\n")
        if stage not in ROLES:
            print(f"[{stage}] {result.get('summary', result.get('log', ''))}", flush=True)

    def agent(self, role):
        result = self.agents.run(role, self.context)
        self.event(role, result)
        return result

    def run(self, task):
        self.context = {"task": task, "attempt": 0, "validation_mode": self.checks.mode}
        if hasattr(self.checks, "describe"):
            self.context["validation_environment"] = self.checks.describe()
        status = "failed"
        try:
            self.context["analysis"] = self.agent("analyst")
            for attempt in range(1, self.max_attempts + 1):
                self.context["attempt"] = attempt
                self.context["implementation"] = self.agent("implementer")
                self.context["checks"] = []
                self.context.pop("failure", None)
                for name in ("build", "test"):
                    result = self.checks.run(name, attempt)
                    self.event(name, result)
                    self.context["checks"].append(result)
                    if not result["passed"]:
                        self.context["failure"] = result
                        break
                if "failure" in self.context:
                    self.context["diagnosis"] = self.agent("diagnostician")
                    continue
                review = self.agent("reviewer")
                self.context["review"] = review
                if review.get("approved") is True:
                    status = "completed"
                    break
                self.context["diagnosis"] = {"summary": "리뷰 수정 요청", "review": review}
            else:
                self.context["error"] = "attempt limit reached"
        except Exception as error:
            self.context["error"] = f"{type(error).__name__}: {error}"
            self.event("error", {"summary": self.context["error"]})
        report = {"status": status, "validation_mode": self.checks.mode, "context": self.context}
        (self.output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        self.event(status, {"summary": f"실행 결과: {status}"})
        return report


def main():
    parser = argparse.ArgumentParser(description="LK AX agent orchestration with stub or QEMU validation")
    parser.add_argument("task")
    parser.add_argument("--scenario", choices=["success", "build-fails-once", "test-fails-once", "always-fail"], default="success")
    parser.add_argument("--agent-config", type=Path, help="JSON mapping roles to executable argument arrays")
    parser.add_argument("--agent-timeout", type=float, default=120)
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--validation", choices=["stub", "qemu"], default="stub")
    parser.add_argument("--lk-root", type=Path, default=Path(__file__).resolve().parents[2] / "lk")
    parser.add_argument("--build-timeout", type=float, default=300)
    parser.add_argument("--test-timeout", type=float, default=90)
    parser.add_argument("--jobs", type=int, default=4)
    parser.add_argument("--cpus", type=int, default=4)
    parser.add_argument("--toolchain-prefix", default="aarch64-linux-gnu-")
    args = parser.parse_args()
    if args.max_attempts < 1 or args.agent_timeout <= 0:
        parser.error("max-attempts and agent-timeout must be positive")
    agents = (CommandAgent(json.loads(args.agent_config.read_text()), args.agent_timeout)
              if args.agent_config else DemoAgent())
    output = args.output or Path("runs") / uuid.uuid4().hex
    if args.validation == "qemu":
        if args.scenario != "success":
            parser.error("--scenario failure simulation is only available with --validation stub")
        from .qemu import QemuChecks
        try:
            checks = QemuChecks(args.lk_root, args.build_timeout, args.test_timeout,
                                args.jobs, args.toolchain_prefix, args.cpus)
        except ValueError as error:
            parser.error(str(error))
    else:
        checks = StubChecks(args.scenario)
    report = Orchestrator(agents, checks, output, args.max_attempts).run(args.task)
    print(f"결과 파일: {output / 'report.json'}")
    raise SystemExit(0 if report["status"] == "completed" else 1)
