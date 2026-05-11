#!/usr/bin/env python
"""No-hardware tests for session-scoped active parameter selection."""

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


class ActiveParameterSerial:
    opened_ports: list[str] = []
    commands: list[str] = []

    def __init__(self, **kwargs) -> None:
        self.port = kwargs["port"]
        self.pending_lines: list[bytes] = []
        self.telemetry_lines: list[bytes] = []
        type(self).opened_ports.append(self.port)

    def __enter__(self) -> "ActiveParameterSerial":
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


class SerialShouldNotOpen:
    opened_ports: list[str] = []

    def __init__(self, **kwargs) -> None:
        type(self).opened_ports.append(str(kwargs.get("port", "")))
        raise AssertionError("active-parameter guard tests must not open serial")


def make_active_plan() -> dict:
    plan = make_plan(port="VIRTUAL-ACTIVE-PARAMETERS")
    template = dict(plan["parameters"][0])
    plan["parameters"] = []
    for key in ["kp_x", "kd_x", "kp_y"]:
        param = dict(template)
        param["key"] = key
        plan["parameters"].append(param)
    plan["step_policy"]["parameter_order"] = ["kp_x", "kd_x", "kp_y"]
    plan["step_policy"]["max_rounds"] = 2
    plan["step_policy"]["cooldown_ms"] = 1
    plan["scoring"]["baseline_window_ms"] = 1
    plan["scoring"]["trial_window_ms"] = 1
    return plan


class TuningSessionActiveParameterTests(unittest.TestCase):
    def setUp(self) -> None:
        ActiveParameterSerial.opened_ports = []
        ActiveParameterSerial.commands = []
        SerialShouldNotOpen.opened_ports = []

    def test_unknown_active_parameter_is_rejected_before_run_without_serial(self) -> None:
        events: list[TuningEvent] = []
        with patch.object(tuning_session.serial, "Serial", SerialShouldNotOpen):
            session = TuningSession(
                make_active_plan(),
                Path("virtual_active_parameters.yaml"),
                event_handler=events.append,
            )
            result = session.set_active_parameters(["kp_x", "missing_gain"])

        self.assertFalse(result.ok)
        self.assertEqual(result.action, "set_active_parameters")
        self.assertEqual(result.state, "idle")
        self.assertEqual(result.error_code, "unknown_parameter_key")
        self.assertEqual(result.data["unknown_parameter_keys"], ["missing_gain"])
        self.assertEqual(SerialShouldNotOpen.opened_ports, [])
        rejected = [event for event in events if event.type == "action_rejected"]
        self.assertEqual(len(rejected), 1)
        self.assertEqual(rejected[0].data["action"], "set_active_parameters")
        self.assertEqual(rejected[0].data["code"], "unknown_parameter_key")

    def test_active_parameter_selection_filters_run_and_does_not_mutate_plan(self) -> None:
        events: list[TuningEvent] = []
        plan = make_active_plan()
        original_plan = copy.deepcopy(plan)
        records: list[dict] = []

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(tuning_session.serial, "Serial", ActiveParameterSerial):
                session = TuningSession(
                    plan,
                    Path("virtual_active_parameters.yaml"),
                    log_dir=Path(tmpdir),
                    event_handler=events.append,
                )
                selection = session.set_active_parameters(["kd_x"])
                exit_code = session.run()
                records = [
                    json.loads(line)
                    for line in Path(session.log_path).read_text(encoding="utf-8").splitlines()
                    if line.strip()
                ]

        self.assertTrue(selection.ok)
        self.assertEqual(selection.data["active_parameter_keys"], ["kd_x"])
        self.assertEqual(selection.data["untouched_parameter_keys"], ["kp_x", "kd_x", "kp_y"])
        self.assertEqual(exit_code, 0)
        self.assertEqual(session.state, "stopped")
        self.assertEqual(ActiveParameterSerial.opened_ports, ["VIRTUAL-ACTIVE-PARAMETERS"])
        self.assertNotIn("COM8", ActiveParameterSerial.opened_ports)
        self.assertEqual(plan, original_plan)

        set_commands = [command for command in ActiveParameterSerial.commands if command.startswith("SET ")]
        self.assertGreaterEqual(len(set_commands), 1)
        self.assertTrue(all(command.startswith("SET kd_x ") for command in set_commands))
        self.assertFalse(any(command.startswith("SET kp_x ") for command in set_commands))
        self.assertFalse(any(command.startswith("SET kp_y ") for command in set_commands))

        action_events = [
            event
            for event in events
            if event.type == "action" and event.data.get("action") == "set_active_parameters"
        ]
        self.assertEqual(len(action_events), 1)
        self.assertEqual(action_events[0].data["active_parameter_keys"], ["kd_x"])

        summary = session.summary()
        self.assertEqual(summary["active_parameter_keys"], ["kd_x"])
        self.assertEqual(summary["skipped_parameter_keys"], [])
        self.assertEqual(summary["touched_parameter_keys"], ["kd_x"])
        self.assertEqual(summary["untouched_parameter_keys"], ["kp_x", "kp_y"])
        self.assertEqual(summary["final_parameters"]["kp_x"], 1.0)
        self.assertGreater(summary["final_parameters"]["kd_x"], 1.0)
        self.assertEqual(summary["final_parameters"]["kp_y"], 1.0)

        summary_records = [record["summary"] for record in records if "summary" in record]
        self.assertEqual(summary_records[-1]["active_parameter_keys"], ["kd_x"])
        self.assertEqual(summary_records[-1]["untouched_parameter_keys"], ["kp_x", "kp_y"])


if __name__ == "__main__":
    unittest.main()
