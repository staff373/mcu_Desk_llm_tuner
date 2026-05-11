#!/usr/bin/env python
"""No-hardware tests for safe TuningSession stop behavior."""

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
        raise AssertionError("safe stop before connection must not open serial")


class SafeStopSerial:
    opened_ports: list[str] = []
    commands: list[str] = []

    def __init__(self, **kwargs) -> None:
        self.port = kwargs["port"]
        self.pending_lines: list[bytes] = []
        self.telemetry_lines: list[bytes] = []
        type(self).opened_ports.append(self.port)

    def __enter__(self) -> "SafeStopSerial":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    def reset_input_buffer(self) -> None:
        self.pending_lines.clear()

    def should_ack(self, command: str) -> bool:
        return True

    def write(self, payload: bytes) -> int:
        command = payload.decode("utf-8").strip()
        type(self).commands.append(command)
        if self.should_ack(command):
            self.pending_lines.append(b"OK\n")
        if command == "TEL_ON":
            self.telemetry_lines = [b"DAT,1.0\n"]
        elif command.startswith("SET "):
            self.telemetry_lines = [b"DAT,0.8\n"]
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


class TimeoutTelemetryOffSerial(SafeStopSerial):
    opened_ports: list[str] = []
    commands: list[str] = []

    def should_ack(self, command: str) -> bool:
        return command != "TEL_OFF"


def make_safe_stop_plan(port: str = "VIRTUAL-SAFE-STOP") -> dict:
    plan = make_plan(port=port)
    plan["step_policy"]["max_rounds"] = 1
    plan["step_policy"]["cooldown_ms"] = 1
    plan["scoring"]["baseline_window_ms"] = 1
    plan["scoring"]["trial_window_ms"] = 1
    return plan


def stop_statuses(session: TuningSession) -> list[str]:
    return [record["status"] for record in session.stop_command_results]


class TuningSessionSafeStopTests(unittest.TestCase):
    def setUp(self) -> None:
        SerialShouldNotOpen.opened_ports = []
        SafeStopSerial.opened_ports = []
        SafeStopSerial.commands = []
        TimeoutTelemetryOffSerial.opened_ports = []
        TimeoutTelemetryOffSerial.commands = []

    def test_stop_before_connection_finishes_without_opening_serial(self) -> None:
        events: list[TuningEvent] = []
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(tuning_session.serial, "Serial", SerialShouldNotOpen):
                session = TuningSession(
                    make_safe_stop_plan(),
                    Path("virtual_safe_stop.yaml"),
                    log_dir=Path(tmpdir),
                    event_handler=events.append,
                )
                result = session.request_stop()
                exit_code = session.run()

        self.assertTrue(result.ok)
        self.assertEqual(exit_code, 0)
        self.assertEqual(session.state, "stopped")
        self.assertEqual(session.stop_reason, "manual_stop")
        self.assertEqual(SerialShouldNotOpen.opened_ports, [])
        self.assertEqual(stop_statuses(session), ["unavailable", "unavailable"])
        self.assertTrue(
            all(record.get("reason") == "no_connection" for record in session.stop_command_results)
        )

        state_pairs = [
            (event.data["previous_state"], event.data["next_state"])
            for event in events
            if event.type == "state"
        ]
        self.assertEqual(state_pairs, [("idle", "stopping"), ("stopping", "stopped")])

    def test_stop_after_connection_attempts_yaml_shutdown_in_order(self) -> None:
        events: list[TuningEvent] = []
        session_holder: dict[str, TuningSession] = {}

        def handle_event(event: TuningEvent) -> None:
            events.append(event)
            if event.type == "state" and event.data["next_state"] == "connected":
                session_holder["session"].request_stop()

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(tuning_session.serial, "Serial", SafeStopSerial):
                session = TuningSession(
                    make_safe_stop_plan(),
                    Path("virtual_safe_stop.yaml"),
                    log_dir=Path(tmpdir),
                    event_handler=handle_event,
                )
                session_holder["session"] = session
                exit_code = session.run()

        self.assertEqual(exit_code, 0)
        self.assertEqual(session.state, "stopped")
        self.assertEqual(SafeStopSerial.opened_ports, ["VIRTUAL-SAFE-STOP"])
        self.assertNotIn("COM8", SafeStopSerial.opened_ports)
        self.assertEqual(SafeStopSerial.commands, ["TEL_OFF", "STOP"])
        self.assertEqual(stop_statuses(session), ["succeeded", "succeeded"])

        stop_tx = [
            event.data["command"]
            for event in events
            if event.type == "tx" and event.data["command"] in {"TEL_OFF", "STOP"}
        ]
        self.assertEqual(stop_tx, ["TEL_OFF", "STOP"])

    def test_stop_during_virtual_tuning_does_not_send_set_after_stop_request(self) -> None:
        events: list[TuningEvent] = []
        session_holder: dict[str, TuningSession] = {}

        def handle_event(event: TuningEvent) -> None:
            events.append(event)
            if event.type == "round":
                session_holder["session"].request_stop()

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(tuning_session.serial, "Serial", SafeStopSerial):
                session = TuningSession(
                    make_safe_stop_plan(),
                    Path("virtual_safe_stop.yaml"),
                    log_dir=Path(tmpdir),
                    event_handler=handle_event,
                )
                session_holder["session"] = session
                exit_code = session.run()

        self.assertEqual(exit_code, 0)
        self.assertEqual(session.state, "stopped")
        self.assertEqual(SafeStopSerial.opened_ports, ["VIRTUAL-SAFE-STOP"])
        self.assertNotIn("COM8", SafeStopSerial.opened_ports)
        self.assertEqual(SafeStopSerial.commands, ["STATUS", "TEL_ON", "TEL_OFF", "STOP"])
        self.assertFalse(any(command.startswith("SET ") for command in SafeStopSerial.commands))
        self.assertEqual(stop_statuses(session), ["succeeded", "succeeded"])

    def test_stop_command_timeout_is_recorded(self) -> None:
        session_holder: dict[str, TuningSession] = {}

        def handle_event(event: TuningEvent) -> None:
            if event.type == "state" and event.data["next_state"] == "connected":
                session_holder["session"].request_stop()

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(tuning_session.serial, "Serial", TimeoutTelemetryOffSerial):
                session = TuningSession(
                    make_safe_stop_plan(),
                    Path("virtual_safe_stop.yaml"),
                    log_dir=Path(tmpdir),
                    event_handler=handle_event,
                )
                session_holder["session"] = session
                exit_code = session.run()

        self.assertEqual(exit_code, 0)
        self.assertEqual(TimeoutTelemetryOffSerial.opened_ports, ["VIRTUAL-SAFE-STOP"])
        self.assertNotIn("COM8", TimeoutTelemetryOffSerial.opened_ports)
        self.assertEqual(TimeoutTelemetryOffSerial.commands, ["TEL_OFF", "STOP"])
        self.assertEqual(stop_statuses(session), ["timed_out", "succeeded"])
        self.assertIn("OK timeout", session.stop_command_results[0]["error"])


if __name__ == "__main__":
    unittest.main()
