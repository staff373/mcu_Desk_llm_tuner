#!/usr/bin/env python
"""No-hardware tests for TuningSession pause and resume controls."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import tuning_session
from test_tuning_session_state import make_plan
from tuning_session import TuningEvent, TuningSession


class PauseResumeSerial:
    opened_ports: list[str] = []
    commands: list[str] = []

    def __init__(self, **kwargs) -> None:
        self.port = kwargs["port"]
        self.pending_lines: list[bytes] = []
        self.telemetry_lines: list[bytes] = []
        type(self).opened_ports.append(self.port)

    def __enter__(self) -> "PauseResumeSerial":
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


def make_pause_plan() -> dict:
    plan = make_plan(port="VIRTUAL-PAUSE-TEST")
    plan["step_policy"]["max_rounds"] = 1
    plan["step_policy"]["cooldown_ms"] = 1
    return plan


def assert_no_set_command_while_paused(testcase: unittest.TestCase, events: list[TuningEvent]) -> None:
    paused = False
    for event in events:
        if event.type == "state" and event.data["next_state"] == "paused":
            paused = True
            continue
        if event.type == "state" and event.data["previous_state"] == "paused":
            paused = False
            continue
        if event.type == "tx" and event.data["command"].startswith("SET "):
            testcase.assertFalse(paused, f"SET command sent while paused: {event.data['command']}")


class TuningSessionPauseResumeTests(unittest.TestCase):
    def setUp(self) -> None:
        PauseResumeSerial.opened_ports = []
        PauseResumeSerial.commands = []

    def test_pause_before_baseline_resumes_to_connected_without_real_serial(self) -> None:
        events: list[TuningEvent] = []
        resume_states: list[str] = []
        session_holder: dict[str, TuningSession] = {}

        def handle_event(event: TuningEvent) -> None:
            events.append(event)
            if event.type == "state" and event.data["next_state"] == "paused":
                resume_states.append(session_holder["session"].resume().state)

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(tuning_session.serial, "Serial", PauseResumeSerial):
                session = TuningSession(
                    make_pause_plan(),
                    Path("virtual_pause_resume.yaml"),
                    log_dir=Path(tmpdir),
                    event_handler=handle_event,
                )
                session_holder["session"] = session
                pause_result = session.pause()
                exit_code = session.run()

        self.assertTrue(pause_result.ok)
        self.assertEqual(exit_code, 0)
        self.assertEqual(resume_states, ["connected"])
        self.assertEqual(session.state, "stopped")
        self.assertEqual(PauseResumeSerial.opened_ports, ["VIRTUAL-PAUSE-TEST"])
        self.assertNotIn("COM8", PauseResumeSerial.opened_ports)

        state_pairs = [
            (event.data["previous_state"], event.data["next_state"])
            for event in events
            if event.type == "state"
        ]
        self.assertIn(("connected", "paused"), state_pairs)
        self.assertIn(("paused", "connected"), state_pairs)
        self.assertTrue(any(event.type == "action" and event.data["action"] == "pause" for event in events))
        self.assertTrue(any(event.type == "action" and event.data["action"] == "resume" for event in events))

    def test_pause_during_tuning_does_not_send_set_while_paused(self) -> None:
        events: list[TuningEvent] = []
        resume_states: list[str] = []
        pause_requested = False
        session_holder: dict[str, TuningSession] = {}

        def handle_event(event: TuningEvent) -> None:
            nonlocal pause_requested
            events.append(event)
            if event.type == "state" and event.data["next_state"] == "tuning" and not pause_requested:
                pause_requested = True
                session_holder["session"].pause()
            elif event.type == "state" and event.data["next_state"] == "paused":
                resume_states.append(session_holder["session"].resume().state)

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(tuning_session.serial, "Serial", PauseResumeSerial):
                session = TuningSession(
                    make_pause_plan(),
                    Path("virtual_pause_resume.yaml"),
                    log_dir=Path(tmpdir),
                    event_handler=handle_event,
                )
                session_holder["session"] = session
                exit_code = session.run()

        self.assertEqual(exit_code, 0)
        self.assertEqual(resume_states, ["tuning"])
        self.assertIn("SET kp_x 1.1", PauseResumeSerial.commands)
        assert_no_set_command_while_paused(self, events)
        self.assertEqual(PauseResumeSerial.opened_ports, ["VIRTUAL-PAUSE-TEST"])
        self.assertNotIn("COM8", PauseResumeSerial.opened_ports)


if __name__ == "__main__":
    unittest.main()
