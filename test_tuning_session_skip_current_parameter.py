#!/usr/bin/env python
"""No-hardware tests for TuningSession skip-current-parameter control."""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import tuning_session
from test_tuning_session_state import make_plan
from tuning_session import TuningEvent, TuningSession


class SkipParameterSerial:
    opened_ports: list[str] = []
    commands: list[str] = []

    def __init__(self, **kwargs) -> None:
        self.port = kwargs["port"]
        self.pending_lines: list[bytes] = []
        self.telemetry_lines: list[bytes] = []
        type(self).opened_ports.append(self.port)

    def __enter__(self) -> "SkipParameterSerial":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    def reset_input_buffer(self) -> None:
        self.pending_lines.clear()

    def write(self, payload: bytes) -> int:
        command = payload.decode("utf-8").strip()
        type(self).commands.append(command)
        self.pending_lines.append(b"OK\n")
        if command == "TEL_ON":
            self.telemetry_lines = [b"DAT,1.0\n", b"DAT,1.0\n"]
        elif command.startswith("SET "):
            self.telemetry_lines = [b"DAT,0.8\n", b"DAT,0.8\n"]
        return len(payload)

    def flush(self) -> None:
        return None

    def readline(self) -> bytes:
        if self.pending_lines:
            return self.pending_lines.pop(0)
        if self.telemetry_lines:
            return self.telemetry_lines.pop(0)
        return b""

    def close(self) -> None:
        return None


def make_skip_plan() -> dict:
    plan = make_plan(port="VIRTUAL-SKIP-TEST")
    kd_x = dict(plan["parameters"][0])
    kd_x["key"] = "kd_x"
    plan["parameters"].append(kd_x)
    plan["step_policy"]["parameter_order"] = ["kp_x", "kd_x"]
    plan["step_policy"]["max_rounds"] = 2
    plan["step_policy"]["cooldown_ms"] = 1
    plan["scoring"]["baseline_window_ms"] = 1
    plan["scoring"]["trial_window_ms"] = 1
    return plan


class TuningSessionSkipCurrentParameterTests(unittest.TestCase):
    def setUp(self) -> None:
        SkipParameterSerial.opened_ports = []
        SkipParameterSerial.commands = []

    def test_skip_current_parameter_during_tuning_skips_only_active_key(self) -> None:
        events: list[TuningEvent] = []
        skip_results = []
        session_holder: dict[str, TuningSession] = {}
        plan = make_skip_plan()
        original_plan = copy.deepcopy(plan)
        records: list[dict] = []

        def handle_event(event: TuningEvent) -> None:
            events.append(event)
            if event.type == "round" and event.data.get("key") == "kp_x" and "trial_value" in event.data:
                skip_results.append(session_holder["session"].skip_current_param("unstable_axis"))

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(tuning_session.serial, "Serial", SkipParameterSerial):
                session = TuningSession(
                    plan,
                    Path("virtual_skip_current_parameter.yaml"),
                    log_dir=Path(tmpdir),
                    event_handler=handle_event,
                )
                session_holder["session"] = session
                exit_code = session.run()
                records = [
                    json.loads(line)
                    for line in Path(session.log_path).read_text(encoding="utf-8").splitlines()
                    if line.strip()
                ]

        self.assertEqual(exit_code, 0)
        self.assertEqual(session.state, "stopped")
        self.assertEqual(SkipParameterSerial.opened_ports, ["VIRTUAL-SKIP-TEST"])
        self.assertNotIn("COM8", SkipParameterSerial.opened_ports)
        self.assertNotIn("SET kp_x 1.1", SkipParameterSerial.commands)
        self.assertIn("SET kd_x 1.1", SkipParameterSerial.commands)
        self.assertEqual(plan, original_plan)

        self.assertEqual(len(skip_results), 1)
        self.assertTrue(skip_results[0].ok)
        self.assertEqual(skip_results[0].data["key"], "kp_x")
        self.assertEqual(skip_results[0].data["reason"], "unstable_axis")

        action_events = [
            event
            for event in events
            if event.type == "action" and event.data.get("action") == "skip_current_param"
        ]
        self.assertEqual(len(action_events), 1)
        self.assertEqual(action_events[0].data["key"], "kp_x")
        self.assertEqual(action_events[0].data["reason"], "unstable_axis")

        summary = session.summary()
        self.assertEqual(summary["skipped_parameter_keys"], ["kp_x"])
        self.assertEqual(summary["skipped_parameters"][0]["key"], "kp_x")
        self.assertEqual(summary["skipped_parameters"][0]["reason"], "unstable_axis")
        self.assertEqual(summary["final_parameters"]["kp_x"], 1.0)
        self.assertEqual(summary["final_parameters"]["kd_x"], 1.1)

        skipped_records = [record for record in records if record.get("decision") == "skipped"]
        self.assertEqual(len(skipped_records), 1)
        self.assertEqual(skipped_records[0]["changed_parameter"], "kp_x")
        self.assertEqual(skipped_records[0]["skip_reason"], "unstable_axis")

        summary_records = [record["summary"] for record in records if "summary" in record]
        self.assertEqual(summary_records[-1]["skipped_parameter_keys"], ["kp_x"])


if __name__ == "__main__":
    unittest.main()
