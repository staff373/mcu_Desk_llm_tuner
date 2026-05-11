#!/usr/bin/env python
"""No-hardware CLI tests for agent_console_probe.py."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class AgentConsoleProbeCliTests(unittest.TestCase):
    def test_probe_cli_runs_virtual_flow_and_writes_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            summary_path = root / "summary.json"
            report_path = root / "report.json"
            result = subprocess.run(
                [
                    sys.executable,
                    "agent_console_probe.py",
                    "--log-dir",
                    str(root),
                    "--summary",
                    str(summary_path),
                    "--report",
                    str(report_path),
                ],
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
            report = json.loads(report_path.read_text(encoding="utf-8"))
            session_log = Path(summary["artifact_paths"]["session_log"])
            self.assertTrue(session_log.exists())

        action_names = [item["action"] for item in summary["action_results"]]
        self.assertTrue(summary["ok"])
        self.assertEqual(summary["missing_required_actions"], [])
        self.assertEqual(summary["missing_required_event_types"], [])
        self.assertEqual(summary["real_serial_status"], "not opened")
        self.assertEqual(summary["serial_open_attempts"], [])
        self.assertTrue(summary["virtual_transport_closed"])
        self.assertTrue(all(summary["control_flags"].values()))
        self.assertTrue(all(action in action_names for action in summary["required_actions"]))
        self.assertGreater(summary["event_counts"]["tx"], 0)
        self.assertGreater(summary["event_counts"]["rx"], 0)
        self.assertGreater(summary["event_counts"]["dat"], 0)
        self.assertIn("final_summary", report)

    def test_probe_cli_exits_nonzero_when_required_event_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            summary_path = root / "summary.json"
            result = subprocess.run(
                [
                    sys.executable,
                    "agent_console_probe.py",
                    "--log-dir",
                    str(root),
                    "--summary",
                    str(summary_path),
                    "--require-event",
                    "definitely_missing_probe_event",
                ],
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("missing required event types", result.stdout)
            summary = json.loads(summary_path.read_text(encoding="utf-8"))

        self.assertIn(
            "definitely_missing_probe_event",
            summary["missing_required_event_types"],
        )
        self.assertEqual(summary["serial_open_attempts"], [])


if __name__ == "__main__":
    unittest.main()
