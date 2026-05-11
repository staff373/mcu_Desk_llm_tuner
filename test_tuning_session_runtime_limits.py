#!/usr/bin/env python
"""No-hardware tests for session-scoped runtime limit overrides."""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import tuning_session
from test_tuning_session_state import make_plan
from tuning_session import TelemetryResult, TuningEvent, TuningSession


class RuntimeLimitSerial:
    opened_ports: list[str] = []
    commands: list[str] = []

    def __init__(self, **kwargs) -> None:
        self.port = kwargs["port"]
        self.pending_lines: list[bytes] = []
        type(self).opened_ports.append(self.port)

    def __enter__(self) -> "RuntimeLimitSerial":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    def reset_input_buffer(self) -> None:
        self.pending_lines.clear()

    def write(self, payload: bytes) -> int:
        command = payload.decode("utf-8").strip()
        type(self).commands.append(command)
        self.pending_lines.append(b"OK\n")
        return len(payload)

    def flush(self) -> None:
        return None

    def readline(self) -> bytes:
        if self.pending_lines:
            return self.pending_lines.pop(0)
        return b""

    def close(self) -> None:
        return None


class SerialShouldNotOpen:
    opened_ports: list[str] = []

    def __init__(self, **kwargs) -> None:
        type(self).opened_ports.append(str(kwargs.get("port", "")))
        raise AssertionError("runtime-limit guard tests must not open serial")


def make_runtime_plan() -> dict:
    plan = make_plan(port="VIRTUAL-RUNTIME-LIMITS")
    plan["step_policy"]["max_rounds"] = 3
    plan["step_policy"]["cooldown_ms"] = 10
    plan["scoring"]["baseline_window_ms"] = 9
    plan["scoring"]["trial_window_ms"] = 20
    return plan


class TuningSessionRuntimeLimitTests(unittest.TestCase):
    def setUp(self) -> None:
        RuntimeLimitSerial.opened_ports = []
        RuntimeLimitSerial.commands = []
        SerialShouldNotOpen.opened_ports = []

    def test_runtime_limit_reductions_apply_to_session_without_mutating_plan(self) -> None:
        events: list[TuningEvent] = []
        telemetry_windows: list[int] = []
        sleep_delays: list[float] = []
        plan = make_runtime_plan()
        original_plan = copy.deepcopy(plan)

        def collect_telemetry(ser, active_plan, window_ms, event_handler):
            telemetry_windows.append(window_ms)
            value = 1.0 if len(telemetry_windows) == 1 else 0.7
            return TelemetryResult([{"error": value}], 0, [f"DAT,{value}"])

        def sleep(delay: float) -> None:
            sleep_delays.append(delay)

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(tuning_session.serial, "Serial", RuntimeLimitSerial):
                with patch.object(tuning_session, "collect_telemetry", side_effect=collect_telemetry):
                    with patch.object(tuning_session.time, "sleep", side_effect=sleep):
                        session = TuningSession(
                            plan,
                            Path("virtual_runtime_limits.yaml"),
                            log_dir=Path(tmpdir),
                            event_handler=events.append,
                        )
                        result = session.set_runtime_limits(
                            max_rounds=1,
                            trial_window_ms=1,
                            cooldown_ms=1,
                        )
                        exit_code = session.run()
                        records = [
                            json.loads(line)
                            for line in Path(session.log_path).read_text(encoding="utf-8").splitlines()
                            if line.strip()
                        ]

        self.assertTrue(result.ok)
        self.assertEqual(exit_code, 0)
        self.assertEqual(session.state, "stopped")
        self.assertEqual(RuntimeLimitSerial.opened_ports, ["VIRTUAL-RUNTIME-LIMITS"])
        self.assertNotIn("COM8", RuntimeLimitSerial.opened_ports)
        self.assertEqual(plan, original_plan)

        set_commands = [command for command in RuntimeLimitSerial.commands if command.startswith("SET ")]
        self.assertEqual(set_commands, ["SET kp_x 1.1"])
        self.assertEqual(telemetry_windows, [9, 1])
        self.assertEqual(sleep_delays, [0.001])

        expected_overrides = {
            "max_rounds": 1,
            "trial_window_ms": 1.0,
            "cooldown_ms": 1.0,
        }
        self.assertEqual(result.data["runtime_limit_overrides"], expected_overrides)
        self.assertEqual(session.summary()["runtime_limit_overrides"], expected_overrides)
        self.assertEqual(session.summary()["yaml_runtime_limits"]["max_rounds"], 3)
        self.assertEqual(session.summary()["effective_runtime_limits"]["max_rounds"], 1)

        action_events = [
            event
            for event in events
            if event.type == "action" and event.data.get("action") == "set_runtime_limits"
        ]
        self.assertEqual(len(action_events), 1)
        self.assertEqual(action_events[0].data["runtime_limit_overrides"], expected_overrides)

        override_records = [
            record
            for record in records
            if record.get("type") == "action" and record.get("action") == "set_runtime_limits"
        ]
        self.assertEqual(len(override_records), 1)
        self.assertEqual(override_records[0]["runtime_limit_overrides"], expected_overrides)
        self.assertEqual(override_records[0]["yaml_runtime_limits"]["trial_window_ms"], 20.0)
        self.assertEqual(override_records[0]["effective_runtime_limits"]["cooldown_ms"], 1.0)

    def test_runtime_limit_expansions_are_rejected_before_serial_open(self) -> None:
        events: list[TuningEvent] = []
        with patch.object(tuning_session.serial, "Serial", SerialShouldNotOpen):
            session = TuningSession(
                make_runtime_plan(),
                Path("virtual_runtime_limits.yaml"),
                event_handler=events.append,
            )
            result = session.set_runtime_limits(
                max_rounds=4,
                trial_window_ms=21,
                cooldown_ms=11,
            )

        self.assertFalse(result.ok)
        self.assertEqual(result.action, "set_runtime_limits")
        self.assertEqual(result.state, "idle")
        self.assertEqual(result.error_code, "runtime_limit_exceeds_yaml")
        self.assertEqual(
            sorted(result.data["exceeded_runtime_limits"]),
            ["cooldown_ms", "max_rounds", "trial_window_ms"],
        )
        self.assertEqual(SerialShouldNotOpen.opened_ports, [])
        rejected = [event for event in events if event.type == "action_rejected"]
        self.assertEqual(len(rejected), 1)
        self.assertEqual(rejected[0].data["action"], "set_runtime_limits")
        self.assertEqual(rejected[0].data["code"], "runtime_limit_exceeds_yaml")


if __name__ == "__main__":
    unittest.main()
