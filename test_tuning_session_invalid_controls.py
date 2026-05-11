#!/usr/bin/env python
"""No-hardware tests for invalid TuningSession control actions."""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

import tuning_session
from test_tuning_session_state import make_plan
from tuning_session import TuningEvent, TuningSession


class SerialShouldNotOpen:
    opened_ports: list[str] = []

    def __init__(self, **kwargs) -> None:
        self.opened_ports.append(str(kwargs.get("port", "")))
        raise AssertionError("invalid control tests must not open serial")


class TuningSessionInvalidControlTests(unittest.TestCase):
    def make_session(self, events: list[TuningEvent]) -> TuningSession:
        return TuningSession(
            make_plan(),
            Path("virtual_invalid_controls.yaml"),
            event_handler=events.append,
        )

    def assert_rejected_event(
        self,
        events: list[TuningEvent],
        *,
        action: str,
        code: str,
        state: str,
    ) -> None:
        rejected = [event for event in events if event.type == "action_rejected"]
        self.assertEqual(len(rejected), 1)
        event = rejected[0]
        self.assertEqual(event.data["action"], action)
        self.assertEqual(event.data["code"], code)
        self.assertEqual(event.data["state"], state)

    def test_resume_when_not_paused_returns_rejected_action(self) -> None:
        events: list[TuningEvent] = []
        SerialShouldNotOpen.opened_ports = []
        with patch.object(tuning_session.serial, "Serial", SerialShouldNotOpen):
            session = self.make_session(events)
            result = session.resume()

        self.assertFalse(result.ok)
        self.assertEqual(result.action, "resume")
        self.assertEqual(result.state, "idle")
        self.assertEqual(result.error_code, "session_not_paused")
        self.assertEqual(SerialShouldNotOpen.opened_ports, [])
        self.assert_rejected_event(events, action="resume", code="session_not_paused", state="idle")

    def test_skip_without_active_parameter_returns_rejected_action(self) -> None:
        events: list[TuningEvent] = []
        SerialShouldNotOpen.opened_ports = []
        with patch.object(tuning_session.serial, "Serial", SerialShouldNotOpen):
            session = self.make_session(events)
            result = session.skip_current_param()

        self.assertFalse(result.ok)
        self.assertEqual(result.action, "skip_current_param")
        self.assertEqual(result.state, "idle")
        self.assertEqual(result.error_code, "no_active_parameter")
        self.assertEqual(SerialShouldNotOpen.opened_ports, [])
        self.assert_rejected_event(events, action="skip_current_param", code="no_active_parameter", state="idle")

    def test_rollback_before_baseline_returns_rejected_action(self) -> None:
        events: list[TuningEvent] = []
        SerialShouldNotOpen.opened_ports = []
        with patch.object(tuning_session.serial, "Serial", SerialShouldNotOpen):
            session = self.make_session(events)
            result = session.rollback_to("baseline")

        self.assertFalse(result.ok)
        self.assertEqual(result.action, "rollback_to")
        self.assertEqual(result.state, "idle")
        self.assertEqual(result.error_code, "baseline_missing")
        self.assertEqual(result.data["target"], "baseline")
        self.assertEqual(SerialShouldNotOpen.opened_ports, [])
        self.assert_rejected_event(events, action="rollback_to", code="baseline_missing", state="idle")


if __name__ == "__main__":
    unittest.main()
