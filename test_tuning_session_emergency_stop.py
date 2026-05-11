#!/usr/bin/env python
"""No-hardware tests for TuningSession emergency stop behavior."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import tuning_session
from test_tuning_session_state import make_plan
from tuning_session import TuningEvent, TuningSession


class SerialShouldNotOpen:
    opened_ports: list[str] = []

    def __init__(self, **kwargs) -> None:
        type(self).opened_ports.append(str(kwargs.get("port", "")))
        raise AssertionError("emergency stop before connection must not open serial")


class EmergencyStopSerial:
    opened_ports: list[str] = []
    commands: list[str] = []

    def __init__(self, **kwargs) -> None:
        self.port = kwargs["port"]
        self.pending_lines: list[bytes] = []
        type(self).opened_ports.append(self.port)

    def __enter__(self) -> "EmergencyStopSerial":
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


def make_emergency_stop_plan(port: str = "VIRTUAL-EMERGENCY-STOP") -> dict:
    plan = make_plan(port=port)
    plan["step_policy"]["max_rounds"] = 1
    plan["step_policy"]["cooldown_ms"] = 1
    plan["scoring"]["baseline_window_ms"] = 1
    plan["scoring"]["trial_window_ms"] = 1
    return plan


def emergency_action_events(events: list[TuningEvent]) -> list[TuningEvent]:
    return [
        event
        for event in events
        if event.type == "action" and event.data.get("action") == "emergency_stop"
    ]


class TuningSessionEmergencyStopTests(unittest.TestCase):
    def setUp(self) -> None:
        SerialShouldNotOpen.opened_ports = []
        EmergencyStopSerial.opened_ports = []
        EmergencyStopSerial.commands = []

    def test_emergency_stop_before_connection_records_operator_abort_without_serial(self) -> None:
        events: list[TuningEvent] = []
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(tuning_session.serial, "Serial", SerialShouldNotOpen):
                session = TuningSession(
                    make_emergency_stop_plan(),
                    Path("virtual_emergency_stop.yaml"),
                    log_dir=Path(tmpdir),
                    event_handler=events.append,
                )
                result = session.emergency_stop()
                exit_code = session.run()

        self.assertTrue(result.ok)
        self.assertEqual(result.action, "emergency_stop")
        self.assertEqual(result.data["stop_reason"], "operator_abort")
        self.assertTrue(result.data["emergency"])
        self.assertEqual(exit_code, 0)
        self.assertEqual(session.state, "stopped")
        self.assertEqual(session.summary()["stop_reason"], "operator_abort")
        self.assertEqual(SerialShouldNotOpen.opened_ports, [])
        self.assertEqual([record["status"] for record in session.stop_command_results], ["unavailable", "unavailable"])

        action = emergency_action_events(events)[0]
        self.assertEqual(action.data["state"], "idle")
        self.assertEqual(action.data["stop_reason"], "operator_abort")
        self.assertTrue(action.data["emergency"])

    def test_emergency_stop_after_connection_uses_only_yaml_shutdown_commands(self) -> None:
        events: list[TuningEvent] = []
        session_holder: dict[str, TuningSession] = {}

        def handle_event(event: TuningEvent) -> None:
            events.append(event)
            if event.type == "state" and event.data["next_state"] == "connected":
                session_holder["session"].emergency_stop()

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(tuning_session.serial, "Serial", EmergencyStopSerial):
                session = TuningSession(
                    make_emergency_stop_plan(),
                    Path("virtual_emergency_stop.yaml"),
                    log_dir=Path(tmpdir),
                    event_handler=handle_event,
                )
                session_holder["session"] = session
                exit_code = session.run()

        self.assertEqual(exit_code, 0)
        self.assertEqual(session.state, "stopped")
        self.assertEqual(session.stop_reason, "operator_abort")
        self.assertEqual(session.summary()["stop_reason"], "operator_abort")
        self.assertEqual(EmergencyStopSerial.opened_ports, ["VIRTUAL-EMERGENCY-STOP"])
        self.assertNotIn("COM8", EmergencyStopSerial.opened_ports)
        self.assertEqual(EmergencyStopSerial.commands, ["TEL_OFF", "STOP"])

        action = emergency_action_events(events)[0]
        self.assertEqual(action.data["state"], "connected")
        self.assertEqual(action.data["session_id"], session.session_id)
        self.assertEqual(action.data["stop_reason"], "operator_abort")
        self.assertFalse(action.data["already_requested"])
        self.assertTrue(action.data["emergency"])

    def test_request_emergency_stop_wrapper_uses_same_session_api(self) -> None:
        events: list[TuningEvent] = []
        with patch.object(tuning_session.serial, "Serial", SerialShouldNotOpen):
            session = TuningSession(
                make_emergency_stop_plan(),
                Path("virtual_emergency_stop.yaml"),
                event_handler=events.append,
            )
            result = session.request_emergency_stop()

        self.assertTrue(result.ok)
        self.assertEqual(result.action, "emergency_stop")
        self.assertEqual(session.stop_reason, "operator_abort")
        self.assertEqual(SerialShouldNotOpen.opened_ports, [])


if __name__ == "__main__":
    unittest.main()
