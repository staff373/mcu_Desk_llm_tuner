#!/usr/bin/env python
"""No-hardware tests for standardized TuningSession error events."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import tuning_session
from test_tuning_session_state import NoTelemetrySerial, make_plan
from tuning_session import ExecutionError, TuningEvent, TuningSession


class SerialOpenFailure:
    opened_ports: list[str] = []

    def __init__(self, **kwargs) -> None:
        type(self).opened_ports.append(str(kwargs.get("port", "")))
        raise tuning_session.serial.SerialException("virtual open failed")


class InitialStatusTimeoutSerial:
    opened_ports: list[str] = []
    commands: list[str] = []

    def __init__(self, **kwargs) -> None:
        self.port = kwargs["port"]
        self.pending_lines: list[bytes] = []
        type(self).opened_ports.append(self.port)

    def __enter__(self) -> "InitialStatusTimeoutSerial":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    def reset_input_buffer(self) -> None:
        self.pending_lines.clear()

    def write(self, payload: bytes) -> int:
        command = payload.decode("utf-8").strip()
        type(self).commands.append(command)
        return len(payload)

    def flush(self) -> None:
        return None

    def readline(self) -> bytes:
        if self.pending_lines:
            return self.pending_lines.pop(0)
        return b""

    def close(self) -> None:
        return None


class ReadbackMismatchSerial:
    opened_ports: list[str] = []
    commands: list[str] = []

    def __init__(self, **kwargs) -> None:
        self.port = kwargs["port"]
        self.pending_lines: list[bytes] = []
        self.telemetry_lines: list[bytes] = []
        self.after_trial_set = False
        type(self).opened_ports.append(self.port)

    def __enter__(self) -> "ReadbackMismatchSerial":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    def reset_input_buffer(self) -> None:
        self.pending_lines.clear()

    def write(self, payload: bytes) -> int:
        command = payload.decode("utf-8").strip()
        type(self).commands.append(command)
        if command == "STATUS" and self.after_trial_set:
            self.pending_lines.extend([b"kp_x=9.9\n", b"OK\n"])
            self.telemetry_lines = [b"DAT,0.8\n"]
        elif command == "STATUS":
            self.pending_lines.append(b"OK\n")
        elif command == "TEL_ON":
            self.pending_lines.append(b"OK\n")
            self.telemetry_lines = [b"DAT,1.0\n"]
        elif command.startswith("SET "):
            self.pending_lines.append(b"OK\n")
            self.after_trial_set = True
        else:
            self.pending_lines.append(b"OK\n")
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


class MalformedTelemetrySerial:
    opened_ports: list[str] = []
    commands: list[str] = []

    def __init__(self, **kwargs) -> None:
        self.port = kwargs["port"]
        self.pending_lines: list[bytes] = []
        self.telemetry_lines: list[bytes] = []
        type(self).opened_ports.append(self.port)

    def __enter__(self) -> "MalformedTelemetrySerial":
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
            self.telemetry_lines = [b"DAT,1.0\n"]
        elif command.startswith("SET "):
            self.telemetry_lines = [b"DAT,0.8\n", b"NOT_TELEMETRY\n"]
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


class RollbackFailureSerial(MalformedTelemetrySerial):
    opened_ports: list[str] = []
    commands: list[str] = []

    def write(self, payload: bytes) -> int:
        command = payload.decode("utf-8").strip()
        type(self).commands.append(command)
        if command.startswith("RB "):
            return len(payload)
        self.pending_lines.append(b"OK\n")
        if command == "TEL_ON":
            self.telemetry_lines = [b"DAT,1.0\n"]
        elif command.startswith("SET "):
            self.telemetry_lines = [b"DAT,2.0\n"]
        return len(payload)


def make_error_plan(port: str) -> dict:
    plan = make_plan(port=port)
    plan["step_policy"]["max_rounds"] = 1
    plan["step_policy"]["cooldown_ms"] = 1
    plan["scoring"]["baseline_window_ms"] = 1
    plan["scoring"]["trial_window_ms"] = 1
    return plan


def error_events(events: list[TuningEvent]) -> list[TuningEvent]:
    return [event for event in events if event.type == "error"]


def error_records(session: TuningSession) -> list[dict]:
    assert session.log_path is not None
    return [
        json.loads(line)
        for line in Path(session.log_path).read_text(encoding="utf-8").splitlines()
        if line.strip() and json.loads(line).get("type") == "error"
    ]


def assert_standard_error(
    test: unittest.TestCase,
    event: TuningEvent,
    record: dict,
    *,
    category: str,
    code: str,
) -> None:
    test.assertEqual(event.type, "error")
    test.assertEqual(event.data["category"], category)
    test.assertEqual(event.data["code"], code)
    test.assertEqual(event.data["error_code"], code)
    test.assertIn("recoverable", event.data)
    test.assertIn("state", event.data)
    test.assertIn("action", event.data)
    test.assertTrue(event.message)
    test.assertEqual(event.data["message"], event.message)

    test.assertEqual(record["category"], category)
    test.assertEqual(record["code"], code)
    test.assertEqual(record["error_code"], code)
    test.assertEqual(record["message"], event.message)
    test.assertIn("recoverable", record)
    test.assertIn("state", record)
    test.assertIn("action", record)


class TuningSessionErrorEventTests(unittest.TestCase):
    def setUp(self) -> None:
        SerialOpenFailure.opened_ports = []
        InitialStatusTimeoutSerial.opened_ports = []
        InitialStatusTimeoutSerial.commands = []
        ReadbackMismatchSerial.opened_ports = []
        ReadbackMismatchSerial.commands = []
        MalformedTelemetrySerial.opened_ports = []
        MalformedTelemetrySerial.commands = []
        RollbackFailureSerial.opened_ports = []
        RollbackFailureSerial.commands = []
        NoTelemetrySerial.opened_ports = []

    def test_serial_exception_emits_standard_error_and_logs_jsonl(self) -> None:
        events: list[TuningEvent] = []
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(tuning_session.serial, "Serial", SerialOpenFailure):
                session = TuningSession(
                    make_error_plan("VIRTUAL-SERIAL-FAIL"),
                    Path("virtual_serial_fail.yaml"),
                    log_dir=Path(tmpdir),
                    event_handler=events.append,
                )
                with self.assertRaises(ExecutionError) as raised:
                    session.run()
            records = error_records(session)

        self.assertEqual(raised.exception.code, "serial_exception")
        self.assertEqual(session.state, "error")
        self.assertEqual(SerialOpenFailure.opened_ports, ["VIRTUAL-SERIAL-FAIL"])
        self.assertNotIn("COM8", SerialOpenFailure.opened_ports)
        assert_standard_error(
            self,
            error_events(events)[0],
            records[0],
            category="serial_exception",
            code="serial_exception",
        )

    def test_ok_timeout_emits_standard_error_and_logs_jsonl(self) -> None:
        events: list[TuningEvent] = []
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(tuning_session.serial, "Serial", InitialStatusTimeoutSerial):
                session = TuningSession(
                    make_error_plan("VIRTUAL-OK-TIMEOUT"),
                    Path("virtual_ok_timeout.yaml"),
                    log_dir=Path(tmpdir),
                    event_handler=events.append,
                )
                with self.assertRaises(ExecutionError) as raised:
                    session.run()
            records = error_records(session)

        self.assertEqual(raised.exception.code, "ok_timeout")
        self.assertEqual(InitialStatusTimeoutSerial.opened_ports, ["VIRTUAL-OK-TIMEOUT"])
        self.assertNotIn("COM8", InitialStatusTimeoutSerial.opened_ports)
        assert_standard_error(
            self,
            error_events(events)[0],
            records[0],
            category="ok_timeout",
            code="ok_timeout",
        )

    def test_readback_mismatch_emits_standard_error_and_logs_jsonl(self) -> None:
        events: list[TuningEvent] = []
        plan = make_error_plan("VIRTUAL-READBACK-MISMATCH")
        plan["commands"]["readback_required"] = True
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(tuning_session.serial, "Serial", ReadbackMismatchSerial):
                session = TuningSession(
                    plan,
                    Path("virtual_readback_mismatch.yaml"),
                    log_dir=Path(tmpdir),
                    event_handler=events.append,
                )
                exit_code = session.run()
            records = error_records(session)

        self.assertEqual(exit_code, 1)
        self.assertEqual(session.state, "error")
        self.assertEqual(ReadbackMismatchSerial.opened_ports, ["VIRTUAL-READBACK-MISMATCH"])
        self.assertNotIn("COM8", ReadbackMismatchSerial.opened_ports)
        assert_standard_error(
            self,
            error_events(events)[0],
            records[0],
            category="readback_mismatch",
            code="readback_mismatch",
        )

    def test_malformed_telemetry_emits_standard_error_and_logs_jsonl(self) -> None:
        events: list[TuningEvent] = []
        plan = make_error_plan("VIRTUAL-MALFORMED-TELEMETRY")
        plan["telemetry"]["malformed_policy"] = "mark_trial_failed"
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(tuning_session.serial, "Serial", MalformedTelemetrySerial):
                session = TuningSession(
                    plan,
                    Path("virtual_malformed_telemetry.yaml"),
                    log_dir=Path(tmpdir),
                    event_handler=events.append,
                )
                exit_code = session.run()
            records = error_records(session)

        self.assertEqual(exit_code, 1)
        self.assertEqual(MalformedTelemetrySerial.opened_ports, ["VIRTUAL-MALFORMED-TELEMETRY"])
        self.assertNotIn("COM8", MalformedTelemetrySerial.opened_ports)
        assert_standard_error(
            self,
            error_events(events)[0],
            records[0],
            category="malformed_telemetry",
            code="malformed_telemetry",
        )

    def test_rollback_failed_emits_standard_error_and_logs_jsonl(self) -> None:
        events: list[TuningEvent] = []
        plan = make_error_plan("VIRTUAL-ROLLBACK-FAIL")
        plan["rollback"] = {"command_template": "RB {key} {value}", "confirm_with": ""}
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(tuning_session.serial, "Serial", RollbackFailureSerial):
                session = TuningSession(
                    plan,
                    Path("virtual_rollback_fail.yaml"),
                    log_dir=Path(tmpdir),
                    event_handler=events.append,
                )
                exit_code = session.run()
            records = error_records(session)

        self.assertEqual(exit_code, 1)
        self.assertEqual(session.stop_reason, "rollback_failed")
        self.assertEqual(RollbackFailureSerial.opened_ports, ["VIRTUAL-ROLLBACK-FAIL"])
        self.assertNotIn("COM8", RollbackFailureSerial.opened_ports)
        assert_standard_error(
            self,
            error_events(events)[0],
            records[0],
            category="rollback_failed",
            code="rollback_failed",
        )

    def test_hard_failure_emits_standard_error_and_logs_jsonl(self) -> None:
        events: list[TuningEvent] = []
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(tuning_session.serial, "Serial", NoTelemetrySerial):
                session = TuningSession(
                    make_error_plan("VIRTUAL-HARD-FAILURE"),
                    Path("virtual_hard_failure.yaml"),
                    log_dir=Path(tmpdir),
                    event_handler=events.append,
                )
                with self.assertRaises(ExecutionError) as raised:
                    session.run()
            records = error_records(session)

        self.assertEqual(raised.exception.category, "hard_failure")
        self.assertEqual(raised.exception.code, "no_telemetry")
        self.assertEqual(NoTelemetrySerial.opened_ports, ["VIRTUAL-HARD-FAILURE"])
        self.assertNotIn("COM8", NoTelemetrySerial.opened_ports)
        assert_standard_error(
            self,
            error_events(events)[0],
            records[0],
            category="hard_failure",
            code="no_telemetry",
        )


if __name__ == "__main__":
    unittest.main()
