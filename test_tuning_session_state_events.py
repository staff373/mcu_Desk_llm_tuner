#!/usr/bin/env python
"""No-hardware tests for structured TuningSession state events."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import tuning_session
from test_tuning_session_state import FakeSerial, make_plan
from tuning_session import TuningEvent, TuningSession


class TuningSessionStateEventTests(unittest.TestCase):
    def test_successful_run_emits_ordered_state_events_and_logs_jsonl(self) -> None:
        events: list[TuningEvent] = []
        FakeSerial.opened_ports = []
        expected_sequence = [
            ("idle", "validating"),
            ("validating", "connected"),
            ("connected", "baseline"),
            ("baseline", "tuning"),
            ("tuning", "stopping"),
            ("stopping", "stopped"),
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(tuning_session.serial, "Serial", FakeSerial):
                session = TuningSession(
                    make_plan(),
                    Path("virtual_state_events.yaml"),
                    log_dir=Path(tmpdir),
                    event_handler=events.append,
                )
                exit_code = session.run()

            log_path = session.log_path
            self.assertIsNotNone(log_path)
            records = [json.loads(line) for line in Path(log_path).read_text(encoding="utf-8").splitlines()]

        self.assertEqual(exit_code, 0)
        self.assertEqual(FakeSerial.opened_ports, ["VIRTUAL-STATE-TEST"])
        self.assertNotIn("COM8", FakeSerial.opened_ports)

        state_events = [event for event in events if event.type == "state"]
        self.assertEqual(
            [(event.data["previous_state"], event.data["next_state"]) for event in state_events],
            expected_sequence,
        )

        for event in state_events:
            self.assertEqual(event.data["session_id"], session.session_id)
            self.assertEqual(event.data["timestamp"], event.timestamp)
            self.assertTrue(event.data["reason"])

        state_records = [record for record in records if record.get("type") == "state"]
        self.assertEqual(
            [(record["previous_state"], record["next_state"]) for record in state_records],
            expected_sequence,
        )
        for record in state_records:
            self.assertEqual(record["session_id"], session.session_id)
            self.assertTrue(record["timestamp"])
            self.assertTrue(record["reason"])


if __name__ == "__main__":
    unittest.main()
