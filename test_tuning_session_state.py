#!/usr/bin/env python
"""No-hardware tests for TuningSession state values."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import tuning_session
from tuning_session import ExecutionError, TuningSession, TuningSessionState


def make_plan(port: str = "VIRTUAL-STATE-TEST") -> dict:
    return {
        "plan_name": "state_test",
        "transport": {
            "port": port,
            "baudrate": 115200,
            "data_bits": 8,
            "parity": "none",
            "stop_bits": 1,
            "line_ending": "\n",
            "read_timeout_ms": 1,
            "write_timeout_ms": 1,
        },
        "commands": {
            "status": "STATUS",
            "telemetry_on": "TEL_ON",
            "telemetry_off": "TEL_OFF",
            "stop": "STOP",
            "set": "SET {key} {value}",
            "ok_pattern": "OK",
            "error_patterns": ["ERR"],
            "readback_required": False,
        },
        "parameters": [
            {
                "key": "kp_x",
                "current": 1.0,
                "min": 0.0,
                "max": 2.0,
                "initial_step": 0.1,
                "max_delta_per_round": 0.1,
            }
        ],
        "telemetry": {
            "format": "csv",
            "prefix": "DAT",
            "delimiter": ",",
            "fields": [{"name": "error", "type": "float", "scale": 1.0}],
        },
        "scoring": {
            "formula": "mean_abs(error)",
            "lower_is_better": True,
            "baseline_window_ms": 1,
            "trial_window_ms": 1,
            "accept_if_improvement_gte": 0.01,
            "rollback_if_degradation_gte": 0.01,
        },
        "step_policy": {
            "parameter_order": ["kp_x"],
            "max_rounds": 0,
            "cooldown_ms": 1,
            "shrink_on_failure": 0.5,
        },
        "rollback": {"command_template": "SET {key} {value}", "confirm_with": ""},
    }


class FakeSerial:
    opened_ports: list[str] = []
    telemetry_lines = [b"DAT,1.0\n"]

    def __init__(self, **kwargs) -> None:
        self.port = kwargs["port"]
        self.pending_lines: list[bytes] = []
        self.commands: list[str] = []
        self.remaining_telemetry = list(self.telemetry_lines)
        type(self).opened_ports.append(self.port)

    def __enter__(self) -> "FakeSerial":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    def reset_input_buffer(self) -> None:
        self.pending_lines.clear()

    def write(self, payload: bytes) -> int:
        command = payload.decode("utf-8").strip()
        self.commands.append(command)
        self.pending_lines.append(b"OK\n")
        return len(payload)

    def flush(self) -> None:
        return None

    def readline(self) -> bytes:
        if self.pending_lines:
            return self.pending_lines.pop(0)
        if self.remaining_telemetry:
            return self.remaining_telemetry.pop(0)
        return b""

    def close(self) -> None:
        return None


class NoTelemetrySerial(FakeSerial):
    opened_ports: list[str] = []
    telemetry_lines: list[bytes] = []


class TuningSessionStateTests(unittest.TestCase):
    def test_initial_state_and_values_are_readable_without_serial(self) -> None:
        session = TuningSession(make_plan(), Path("virtual_state_test.yaml"))

        self.assertEqual(session.state, TuningSessionState.IDLE)
        self.assertEqual(session.STATE_IDLE, "idle")
        self.assertEqual(
            tuple(session.STATE_VALUES),
            (
                "idle",
                "validating",
                "connected",
                "baseline",
                "tuning",
                "paused",
                "rollback",
                "stopping",
                "stopped",
                "error",
            ),
        )

    def test_successful_run_finishes_in_stopped_state_without_real_serial(self) -> None:
        FakeSerial.opened_ports = []
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(tuning_session.serial, "Serial", FakeSerial):
                session = TuningSession(make_plan(), Path("virtual_state_test.yaml"), log_dir=Path(tmpdir))
                exit_code = session.run()

        self.assertEqual(exit_code, 0)
        self.assertEqual(session.state, TuningSessionState.STOPPED)
        self.assertEqual(FakeSerial.opened_ports, ["VIRTUAL-STATE-TEST"])
        self.assertNotIn("COM8", FakeSerial.opened_ports)

    def test_failed_run_finishes_in_error_state_without_real_serial(self) -> None:
        NoTelemetrySerial.opened_ports = []
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(tuning_session.serial, "Serial", NoTelemetrySerial):
                session = TuningSession(make_plan(), Path("virtual_state_test.yaml"), log_dir=Path(tmpdir))
                with self.assertRaises(ExecutionError):
                    session.run()

        self.assertEqual(session.state, TuningSessionState.ERROR)
        self.assertEqual(NoTelemetrySerial.opened_ports, ["VIRTUAL-STATE-TEST"])
        self.assertNotIn("COM8", NoTelemetrySerial.opened_ports)


if __name__ == "__main__":
    unittest.main()
