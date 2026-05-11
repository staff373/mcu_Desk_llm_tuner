#!/usr/bin/env python
"""No-hardware tests for manual TuningSession rollback targets."""

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
        raise AssertionError("rollback guard tests must not open serial")


class RollbackSerial:
    opened_ports: list[str] = []
    commands: list[str] = []

    def __init__(self, **kwargs) -> None:
        self.port = kwargs["port"]
        self.pending_lines: list[bytes] = []
        self.telemetry_lines: list[bytes] = []
        type(self).opened_ports.append(self.port)

    def __enter__(self) -> "RollbackSerial":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    def reset_input_buffer(self) -> None:
        self.pending_lines.clear()
        self.telemetry_lines.clear()

    def should_ack(self, command: str) -> bool:
        return True

    def write(self, payload: bytes) -> int:
        command = payload.decode("utf-8").strip()
        type(self).commands.append(command)
        if self.should_ack(command):
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


class TimeoutRollbackSerial(RollbackSerial):
    opened_ports: list[str] = []
    commands: list[str] = []

    def should_ack(self, command: str) -> bool:
        return not command.startswith("RB ")


def make_rollback_plan(port: str = "VIRTUAL-ROLLBACK-TEST") -> dict:
    plan = make_plan(port=port)
    plan["rollback"] = {"command_template": "RB {key} {value}", "confirm_with": ""}
    plan["step_policy"]["max_rounds"] = 1
    plan["step_policy"]["cooldown_ms"] = 1
    plan["scoring"]["baseline_window_ms"] = 1
    plan["scoring"]["trial_window_ms"] = 1
    return plan


def rollback_action_events(events: list[TuningEvent]) -> list[TuningEvent]:
    return [
        event
        for event in events
        if event.type == "action" and event.data.get("action") == "rollback_to"
    ]


def command_has_rx(events: list[TuningEvent], command: str, expected_line: str) -> bool:
    for index, event in enumerate(events):
        if event.type == "tx" and event.data.get("command") == command:
            return any(
                later.type == "rx" and later.data.get("line") == expected_line
                for later in events[index + 1 :]
            )
    return False


class TuningSessionRollbackTargetTests(unittest.TestCase):
    def setUp(self) -> None:
        SerialShouldNotOpen.opened_ports = []
        RollbackSerial.opened_ports = []
        RollbackSerial.commands = []
        TimeoutRollbackSerial.opened_ports = []
        TimeoutRollbackSerial.commands = []

    def test_rollback_before_baseline_returns_rejected_action_without_serial(self) -> None:
        events: list[TuningEvent] = []
        with patch.object(tuning_session.serial, "Serial", SerialShouldNotOpen):
            session = TuningSession(
                make_rollback_plan(),
                Path("virtual_rollback_targets.yaml"),
                event_handler=events.append,
            )
            result = session.rollback_to("last_stable")

        self.assertFalse(result.ok)
        self.assertEqual(result.action, "rollback_to")
        self.assertEqual(result.error_code, "baseline_missing")
        self.assertEqual(result.data["target"], "last_stable")
        self.assertEqual(SerialShouldNotOpen.opened_ports, [])
        rejected = [event for event in events if event.type == "action_rejected"]
        self.assertEqual(rejected[0].data["code"], "baseline_missing")

    def test_rollback_to_last_stable_uses_yaml_template_and_restores_current(self) -> None:
        events: list[TuningEvent] = []
        results = []
        session_holder: dict[str, TuningSession] = {}

        def handle_event(event: TuningEvent) -> None:
            events.append(event)
            if event.type == "decision" and event.data.get("decision") == "accept":
                session = session_holder["session"]
                session.current["kp_x"] = 1.6
                results.append(session.rollback_to("last_stable"))
                session.request_stop()

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(tuning_session.serial, "Serial", RollbackSerial):
                session = TuningSession(
                    make_rollback_plan(),
                    Path("virtual_rollback_targets.yaml"),
                    log_dir=Path(tmpdir),
                    event_handler=handle_event,
                )
                session_holder["session"] = session
                exit_code = session.run()

        self.assertEqual(exit_code, 0)
        self.assertEqual(session.state, "stopped")
        self.assertEqual(RollbackSerial.opened_ports, ["VIRTUAL-ROLLBACK-TEST"])
        self.assertNotIn("COM8", RollbackSerial.opened_ports)
        self.assertIn("RB kp_x 1.1", RollbackSerial.commands)
        self.assertTrue(command_has_rx(events, "RB kp_x 1.1", "OK"))
        self.assertEqual(session.summary()["final_parameters"]["kp_x"], 1.1)

        self.assertEqual(len(results), 1)
        self.assertTrue(results[0].ok)
        self.assertEqual(results[0].data["target"], "last_stable")
        self.assertEqual(results[0].data["restored_parameters"], {"kp_x": 1.1})

        actions = rollback_action_events(events)
        self.assertEqual(len(actions), 1)
        self.assertTrue(actions[0].data["ok"])
        self.assertEqual(actions[0].data["commands"][0]["command"], "RB kp_x 1.1")

    def test_rollback_to_baseline_uses_yaml_template_and_restores_current(self) -> None:
        events: list[TuningEvent] = []
        results = []
        session_holder: dict[str, TuningSession] = {}

        def handle_event(event: TuningEvent) -> None:
            events.append(event)
            if event.type == "decision" and event.data.get("decision") == "accept":
                session = session_holder["session"]
                results.append(session.rollback_to("baseline"))
                session.request_stop()

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(tuning_session.serial, "Serial", RollbackSerial):
                session = TuningSession(
                    make_rollback_plan(),
                    Path("virtual_rollback_targets.yaml"),
                    log_dir=Path(tmpdir),
                    event_handler=handle_event,
                )
                session_holder["session"] = session
                exit_code = session.run()

        self.assertEqual(exit_code, 0)
        self.assertEqual(session.state, "stopped")
        self.assertEqual(RollbackSerial.opened_ports, ["VIRTUAL-ROLLBACK-TEST"])
        self.assertNotIn("COM8", RollbackSerial.opened_ports)
        self.assertIn("RB kp_x 1.0", RollbackSerial.commands)
        self.assertTrue(command_has_rx(events, "RB kp_x 1.0", "OK"))
        self.assertEqual(session.summary()["final_parameters"]["kp_x"], 1.0)

        self.assertEqual(len(results), 1)
        self.assertTrue(results[0].ok)
        self.assertEqual(results[0].data["target"], "baseline")
        self.assertEqual(results[0].data["restored_parameters"], {"kp_x": 1.0})

        actions = rollback_action_events(events)
        self.assertEqual(len(actions), 1)
        self.assertTrue(actions[0].data["ok"])
        self.assertEqual(actions[0].data["commands"][0]["command"], "RB kp_x 1.0")

    def test_rollback_command_timeout_moves_session_to_error(self) -> None:
        events: list[TuningEvent] = []
        results = []
        session_holder: dict[str, TuningSession] = {}

        def handle_event(event: TuningEvent) -> None:
            events.append(event)
            if event.type == "baseline":
                results.append(session_holder["session"].rollback_to("last_stable"))

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(tuning_session.serial, "Serial", TimeoutRollbackSerial):
                session = TuningSession(
                    make_rollback_plan("VIRTUAL-ROLLBACK-TIMEOUT"),
                    Path("virtual_rollback_timeout.yaml"),
                    log_dir=Path(tmpdir),
                    event_handler=handle_event,
                )
                session_holder["session"] = session
                exit_code = session.run()

        self.assertEqual(exit_code, 1)
        self.assertEqual(session.state, "error")
        self.assertEqual(session.stop_reason, "rollback_failed")
        self.assertEqual(TimeoutRollbackSerial.opened_ports, ["VIRTUAL-ROLLBACK-TIMEOUT"])
        self.assertNotIn("COM8", TimeoutRollbackSerial.opened_ports)
        self.assertIn("RB kp_x 1.0", TimeoutRollbackSerial.commands)

        self.assertEqual(len(results), 1)
        self.assertFalse(results[0].ok)
        self.assertEqual(results[0].error_code, "rollback_failed")
        self.assertEqual(results[0].data["failed_key"], "kp_x")
        self.assertIn("OK timeout", results[0].message)

        actions = rollback_action_events(events)
        self.assertEqual(len(actions), 1)
        self.assertFalse(actions[0].data["ok"])
        self.assertEqual(actions[0].data["error_code"], "rollback_failed")


if __name__ == "__main__":
    unittest.main()
