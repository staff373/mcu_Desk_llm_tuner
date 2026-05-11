#!/usr/bin/env python
"""Tests for verify_no_hardware.py without spawning the full harness."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import verify_no_hardware as harness


class FakeRunner:
    def __init__(self, *, serial_attempts: list[str] | None = None) -> None:
        self.serial_attempts = serial_attempts or []
        self.step_names: list[str] = []

    def __call__(self, step: harness.VerificationStep, cwd: Path) -> harness.StepResult:
        self.step_names.append(step.name)
        if step.name == "agent_probe":
            summary_path = Path(step.metadata["summary_path"])
            report_path = Path(step.metadata["report_path"])
            summary_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.parent.mkdir(parents=True, exist_ok=True)
            summary_path.write_text(
                json.dumps(
                    {
                        "ok": True,
                        "real_serial_status": (
                            "not opened" if not self.serial_attempts else "open attempted"
                        ),
                        "serial_open_attempts": list(self.serial_attempts),
                        "event_counts": {"tx": 1, "rx": 1, "dat": 1},
                        "required_actions": ["load_plan"],
                        "missing_required_actions": [],
                        "missing_required_event_types": [],
                        "virtual_transport_closed": True,
                    }
                ),
                encoding="utf-8",
            )
            report_path.write_text("{}", encoding="utf-8")
        return harness.StepResult(
            name=step.name,
            command=step.command,
            returncode=0,
            duration_seconds=0.001,
            stdout="ok",
        )


class NoHardwareVerificationHarnessTests(unittest.TestCase):
    def test_run_verification_writes_summary_and_cleans_temporary_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            temp_root = Path(tmpdir)
            summary_path = temp_root / "summary.json"
            artifact_dir = temp_root / "artifacts"
            project_pycache = Path.cwd() / "__pycache__"
            project_pycache.mkdir(exist_ok=True)
            (project_pycache / "temporary_test_cache.pyc").write_bytes(b"cache")
            runner = FakeRunner()

            exit_code = harness.run_verification(
                root=Path.cwd(),
                summary_path=summary_path,
                artifact_dir=artifact_dir,
                cleanup_artifacts=True,
                runner=runner,
            )

            data = json.loads(summary_path.read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 0)
        self.assertTrue(data["ok"])
        self.assertEqual(data["real_serial_status"], "not opened")
        self.assertEqual(data["serial_open_attempts"], [])
        self.assertIn("artifact_dir_removed", data["cleanup"])
        self.assertFalse(artifact_dir.exists())
        self.assertIn("py_compile", runner.step_names)
        self.assertIn("validate_virtual_plan", runner.step_names)
        self.assertIn("run_cli_help", runner.step_names)
        self.assertIn("monitor_cli_help", runner.step_names)
        self.assertIn("gui_self_test", runner.step_names)
        self.assertIn("gui_hidden_init", runner.step_names)
        self.assertIn("gui_hidden_init_no_serial_guard", runner.step_names)
        self.assertIn("agent_probe", runner.step_names)

    def test_run_verification_fails_when_probe_reports_serial_open_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            temp_root = Path(tmpdir)
            summary_path = temp_root / "summary.json"
            artifact_dir = temp_root / "artifacts"
            runner = FakeRunner(serial_attempts=["COM8"])

            exit_code = harness.run_verification(
                root=Path.cwd(),
                summary_path=summary_path,
                artifact_dir=artifact_dir,
                cleanup_artifacts=True,
                runner=runner,
            )
            data = json.loads(summary_path.read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 1)
        self.assertFalse(data["ok"])
        self.assertEqual(data["real_serial_status"], "open attempted")
        self.assertEqual(data["serial_open_attempts"], ["COM8"])


if __name__ == "__main__":
    unittest.main()
