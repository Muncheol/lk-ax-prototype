"""Build LK and validate its unit tests on QEMU ARM64."""

import os
import re
import signal
import subprocess
import time
from pathlib import Path


class QemuChecks:
    mode = "qemu"
    project = "qemu-virt-arm64-test"

    def __init__(self, lk_root, build_timeout=300, test_timeout=90, jobs=4,
                 toolchain_prefix="aarch64-linux-gnu-", cpus=4):
        self.lk_root = Path(lk_root).resolve()
        if not (self.lk_root / "makefile").is_file():
            raise ValueError(f"LK makefile not found: {self.lk_root}")
        if min(build_timeout, test_timeout, jobs, cpus) <= 0:
            raise ValueError("timeouts, jobs and cpus must be positive")
        self.build_timeout, self.test_timeout = build_timeout, test_timeout
        self.jobs, self.toolchain_prefix, self.cpus = jobs, toolchain_prefix, cpus

    def bind_output(self, output):
        self.output = Path(output).resolve()
        self.build_root = self.output / "build"
        self.build_dir = self.build_root / f"build-{self.project}"

    def describe(self):
        return {"lk_root": str(self.lk_root), "project": self.project,
                "toolchain": "gcc", "toolchain_prefix": self.toolchain_prefix,
                "compiler_flags": ["-Ulinux", "-U__linux", "-U__linux__", "-mno-outline-atomics"],
                "heap": "miniheap", "cpus": self.cpus,
                "build_root": str(self.build_root)}

    def command(self, name):
        if name == "build":
            return ["make", f"-j{self.jobs}", self.project, "TOOLCHAIN=gcc",
                    f"TOOLCHAIN_PREFIX={self.toolchain_prefix}", "IGNORE_LOCAL_MK=1",
                    f"CC={self.toolchain_prefix}gcc -Ulinux -U__linux -U__linux__ -mno-outline-atomics",
                    "LK_HEAP_IMPLEMENTATION=miniheap", "DEBUG=2", "UBSAN=0",
                    f"BUILDROOT={self.build_root}", "BUILDDIR_SUFFIX="]
        if name == "test":
            return ["qemu-system-aarch64", "-machine", "virt,gic-version=2",
                    "-cpu", "cortex-a76", "-m", "512", "-smp", str(self.cpus),
                    "-nographic", "-monitor", "none", "-net", "none",
                    "-kernel", str(self.build_dir / "lk.elf"),
                    "-append", "lk.autorun=sleep+5;ut+all;poweroff"]
        raise ValueError(f"Unknown check: {name}")

    def run(self, name, attempt):
        command = self.command(name)
        self.build_root.mkdir(parents=True, exist_ok=True)
        log_path = self.output / f"{name}-attempt-{attempt}.log"
        timeout = self.build_timeout if name == "build" else self.test_timeout
        print(f"[{name}] 실제 실행 시작 (시도 {attempt}, timeout {timeout}s)", flush=True)
        started = time.monotonic()
        timed_out, returncode, error = False, None, None
        env = os.environ.copy()
        env.update({"BUILDROOT": str(self.build_root), "BUILDDIR_SUFFIX": "", "LC_ALL": "C"})
        with log_path.open("w", encoding="utf-8") as stream:
            try:
                process = subprocess.Popen(command, cwd=self.lk_root, env=env,
                                           stdin=subprocess.DEVNULL, stdout=stream,
                                           stderr=subprocess.STDOUT, start_new_session=True)
                try:
                    returncode = process.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    timed_out = True
                    os.killpg(process.pid, signal.SIGKILL)
                    returncode = process.wait()
                except BaseException:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
                    raise
            except OSError as exc:
                error = str(exc)
                stream.write(error + "\n")
        text = log_path.read_text(encoding="utf-8", errors="replace")
        passed = returncode == 0 and not timed_out and error is None
        result = {"name": name, "stub": False, "command": command,
                  "cwd": str(self.lk_root), "exit_code": returncode,
                  "timed_out": timed_out, "duration_seconds": round(time.monotonic() - started, 3),
                  "log_path": str(log_path), "log_tail": text[-12000:]}
        reason = error or ("timeout" if timed_out else f"exit={returncode}")
        if name == "build":
            artifacts = [self.build_dir / "lk.elf", self.build_dir / "lk.bin"]
            result["artifacts"] = [str(path) for path in artifacts]
            if passed and not all(path.is_file() and path.stat().st_size for path in artifacts):
                passed, reason = False, "missing or empty build artifacts"
        else:
            summary = re.search(r"CASES:\s*(\d+)\s+SUCCESS:\s*(\d+)\s+FAILED:\s*(\d+)", text)
            counts = dict(zip(("total", "passed", "failed"), map(int, summary.groups()))) if summary else None
            result["test_cases"] = counts
            markers_ok = (counts is not None and counts["total"] > 0
                          and counts["total"] == counts["passed"] and counts["failed"] == 0
                          and "welcome to lk" in text and "SUCCESS! All test cases passed" in text
                          and not re.search(r"FAILURE!|\[FAILED\]|PANIC|panic\(", text))
            if passed and not markers_ok:
                passed, reason = False, "missing successful LK test summary or guest failure detected"
        result["passed"] = passed
        case_summary = ""
        if name == "test" and result["test_cases"]:
            counts = result["test_cases"]
            case_summary = f"; cases={counts['total']}, passed={counts['passed']}, failed={counts['failed']}"
        result["log"] = f"[QEMU] {name}: {'PASS' if passed else 'FAIL'} ({reason}){case_summary}; log={log_path}"
        return result
