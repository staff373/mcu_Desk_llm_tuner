#!/usr/bin/env python
"""Run the project no-hardware regression verification gate."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Sequence


PROJECT_ROOT = Path(__file__).resolve().parent
VIRTUAL_PLAN = Path("fixtures") / "virtual_mcu_tuning_plan.yaml"
DEFAULT_SUMMARY = Path("reports") / "no_hardware_verification_summary.json"
GLOBAL_EXECUTOR_SKILL = Path(r"C:\Users\1\.codex\skills\mcu-bluetooth-executor")
QUICK_VALIDATE = Path(r"C:\Users\1\.codex\skills\.system\skill-creator\scripts\quick_validate.py")

HIDDEN_INIT_NO_SERIAL_SCRIPT = r"""
from unittest.mock import patch

import tuning_session
from tuning_panel import TuningPanel

opened_ports = []


class SerialShouldNotOpen:
    def __init__(self, *args, **kwargs):
        port = kwargs.get("port")
        if port is None and args:
            port = args[0]
        opened_ports.append(str(port or ""))
        raise AssertionError("hidden GUI initialization attempted to open serial")


with patch.object(tuning_session.serial, "Serial", SerialShouldNotOpen):
    app = TuningPanel(run_backend="session")
    try:
        app.withdraw()
        app.update_idletasks()
    finally:
        app.destroy()

if opened_ports:
    raise SystemExit("real serial open attempted: " + ", ".join(opened_ports))

print("HIDDEN_INIT_GUARD: serial_open_attempts=[]")
"""


@dataclass(frozen=True)
class VerificationStep:
    name: str
    command: list[str]
    timeout_seconds: int = 120
    metadata: dict[str, str] = field(default_factory=dict)


@dataclass
class StepResult:
    name: str
    command: list[str]
    returncode: int
    duration_seconds: float
    stdout: str = ""
    stderr: str = ""
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and self.error is None

    def to_summary(self) -> dict[str, object]:
        return {
            "name": self.name,
            "command": self.command,
            "returncode": self.returncode,
            "duration_seconds": round(self.duration_seconds, 3),
            "ok": self.ok,
            "stdout_tail": _tail(self.stdout),
            "stderr_tail": _tail(self.stderr),
            "error": self.error,
        }


Runner = Callable[[VerificationStep, Path], StepResult]


def _tail(text: str, *, max_chars: int = 1200) -> str:
    text = text.strip()
    if len(text) <= max_chars:
        return text
    return text[-max_chars:]


def _project_python_files(root: Path) -> list[str]:
    return [
        str(path.relative_to(root))
        for path in sorted(root.glob("*.py"))
        if path.is_file()
    ]


def _build_steps(root: Path, artifact_dir: Path) -> tuple[list[VerificationStep], list[dict[str, str]]]:
    probe_dir = artifact_dir / "agent_probe"
    probe_summary = artifact_dir / "agent_probe_summary.json"
    probe_report = artifact_dir / "agent_probe_report.json"
    steps = [
        VerificationStep(
            name="py_compile",
            command=[sys.executable, "-m", "py_compile", *_project_python_files(root), "scripts/open_tuning_console.py"],
            timeout_seconds=180,
        ),
        VerificationStep(
            name="validate_virtual_plan",
            command=[sys.executable, "validate_plan.py", str(VIRTUAL_PLAN)],
        ),
        VerificationStep(
            name="run_cli_help",
            command=[sys.executable, "run_tuning_plan.py", "--help"],
        ),
        VerificationStep(
            name="monitor_cli_help",
            command=[sys.executable, "monitor_tuning_plan.py", "--help"],
        ),
        VerificationStep(
            name="gui_self_test",
            command=[sys.executable, "tuning_panel.py", "--self-test"],
        ),
        VerificationStep(
            name="gui_hidden_init",
            command=[sys.executable, "tuning_panel.py", "--hidden-init-test"],
        ),
        VerificationStep(
            name="gui_hidden_init_no_serial_guard",
            command=[sys.executable, "-c", HIDDEN_INIT_NO_SERIAL_SCRIPT],
        ),
        VerificationStep(
            name="open_console_launcher_no_launch",
            command=[sys.executable, "scripts/open_tuning_console.py", str(VIRTUAL_PLAN), "--no-launch"],
        ),
        VerificationStep(
            name="agent_probe",
            command=[
                sys.executable,
                "agent_console_probe.py",
                "--log-dir",
                str(probe_dir),
                "--summary",
                str(probe_summary),
                "--report",
                str(probe_report),
            ],
            timeout_seconds=180,
            metadata={
                "summary_path": str(probe_summary),
                "report_path": str(probe_report),
                "artifact_dir": str(probe_dir),
            },
        ),
    ]

    skipped: list[dict[str, str]] = []
    if GLOBAL_EXECUTOR_SKILL.exists() and QUICK_VALIDATE.exists():
        steps.append(
            VerificationStep(
                name="executor_skill_quick_validate",
                command=[sys.executable, str(QUICK_VALIDATE), str(GLOBAL_EXECUTOR_SKILL)],
            )
        )
    else:
        skipped.append(
            {
                "name": "executor_skill_quick_validate",
                "reason": "global executor skill or quick_validate.py not present",
            }
        )

    return steps, skipped


def run_process(step: VerificationStep, cwd: Path) -> StepResult:
    start = time.monotonic()
    try:
        completed = subprocess.run(
            step.command,
            cwd=cwd,
            check=False,
            capture_output=True,
            text=True,
            timeout=step.timeout_seconds,
        )
        return StepResult(
            name=step.name,
            command=step.command,
            returncode=completed.returncode,
            duration_seconds=time.monotonic() - start,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )
    except subprocess.TimeoutExpired as exc:
        return StepResult(
            name=step.name,
            command=step.command,
            returncode=124,
            duration_seconds=time.monotonic() - start,
            stdout=exc.stdout or "",
            stderr=exc.stderr or "",
            error=f"timeout after {step.timeout_seconds}s",
        )


def _read_json(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return payload


def _assert_no_real_serial(probe_summary: dict[str, object]) -> tuple[bool, str, list[str]]:
    attempts = probe_summary.get("serial_open_attempts", [])
    if not isinstance(attempts, list):
        return False, "unknown", ["serial_open_attempts was not a list"]
    attempt_values = [str(item) for item in attempts]
    status = str(probe_summary.get("real_serial_status", "unknown"))
    if status != "not opened" or attempt_values:
        return False, status, attempt_values
    return True, status, attempt_values


def _clean_pycache_dirs(root: Path) -> list[str]:
    removed: list[str] = []
    for path in sorted(root.rglob("__pycache__")):
        if not path.is_dir():
            continue
        shutil.rmtree(path)
        removed.append(str(path.relative_to(root)))
    return removed


def run_verification(
    *,
    root: Path,
    summary_path: Path,
    artifact_dir: Path,
    cleanup_artifacts: bool,
    runner: Runner = run_process,
) -> int:
    root = root.resolve()
    summary_path = summary_path.resolve()
    artifact_dir = artifact_dir.resolve()
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_dir.mkdir(parents=True, exist_ok=True)

    steps, skipped = _build_steps(root, artifact_dir)
    results: list[StepResult] = []
    probe_summary: dict[str, object] = {}
    ok = True
    failure_reason = ""
    real_serial_status = "not verified"
    serial_open_attempts: list[str] = []
    cleanup: dict[str, object] = {}

    try:
        for step in steps:
            result = runner(step, root)
            results.append(result)
            if not result.ok:
                ok = False
                failure_reason = f"{step.name} failed"
                break

        if ok:
            probe_step = next(step for step in steps if step.name == "agent_probe")
            probe_summary = _read_json(Path(probe_step.metadata["summary_path"]))
            no_serial_ok, real_serial_status, serial_open_attempts = _assert_no_real_serial(
                probe_summary
            )
            if not no_serial_ok:
                ok = False
                failure_reason = "agent probe reported real serial open attempts"
    except Exception as exc:
        ok = False
        failure_reason = f"{type(exc).__name__}: {exc}"
    finally:
        cleanup["removed_pycache"] = _clean_pycache_dirs(root)
        if cleanup_artifacts and artifact_dir.exists():
            shutil.rmtree(artifact_dir)
            cleanup["artifact_dir_removed"] = str(artifact_dir)
        else:
            cleanup["artifact_dir_preserved"] = str(artifact_dir)

    summary = {
        "schema": "no_hardware_verification_summary_v1",
        "ok": ok,
        "timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
        "root": str(root),
        "summary_path": str(summary_path),
        "artifact_dir": str(artifact_dir),
        "failure_reason": failure_reason,
        "real_serial_status": real_serial_status,
        "serial_open_attempts": serial_open_attempts,
        "checks": [result.to_summary() for result in results],
        "skipped_checks": skipped,
        "probe_summary": {
            "ok": probe_summary.get("ok"),
            "event_counts": probe_summary.get("event_counts"),
            "required_actions": probe_summary.get("required_actions"),
            "missing_required_actions": probe_summary.get("missing_required_actions"),
            "missing_required_event_types": probe_summary.get("missing_required_event_types"),
            "virtual_transport_closed": probe_summary.get("virtual_transport_closed"),
        },
        "cleanup": cleanup,
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if ok else 1


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the no-hardware regression gate for the MCU tuning console."
    )
    parser.add_argument(
        "--summary",
        type=Path,
        default=DEFAULT_SUMMARY,
        help="Path for the concise verification summary JSON.",
    )
    parser.add_argument(
        "--artifact-dir",
        type=Path,
        default=None,
        help="Optional directory for probe artifacts. Defaults to a temporary directory.",
    )
    parser.add_argument(
        "--keep-artifacts",
        action="store_true",
        help="Keep temporary probe artifacts instead of cleaning them after the run.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    owned_artifact_dir = args.artifact_dir is None
    artifact_dir = args.artifact_dir or Path(
        tempfile.mkdtemp(prefix="mcu_no_hardware_verify_")
    )
    summary_path = args.summary
    if not summary_path.is_absolute():
        summary_path = PROJECT_ROOT / summary_path
    if not artifact_dir.is_absolute():
        artifact_dir = PROJECT_ROOT / artifact_dir

    exit_code = run_verification(
        root=PROJECT_ROOT,
        summary_path=summary_path,
        artifact_dir=artifact_dir,
        cleanup_artifacts=owned_artifact_dir and not args.keep_artifacts,
    )
    status = "ok" if exit_code == 0 else "failed"
    print(f"NO_HARDWARE_VERIFY: {status} summary={summary_path}")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
