#!/usr/bin/env python
"""No-hardware tests for deterministic virtual scoring behavior."""

from __future__ import annotations

import tempfile
import unittest
from collections import Counter
from pathlib import Path
from unittest.mock import patch

import tuning_session
from validate_plan import load_plan
from tuning_session import (
    TuningEvent,
    TuningSession,
    VirtualTransport,
    collect_telemetry,
    format_template,
    score_window,
    send_command,
)


FIXTURE_PATH = Path("fixtures/virtual_mcu_tuning_plan.yaml")


class SerialShouldNotOpen:
    opened_ports: list[str] = []

    def __init__(self, **kwargs) -> None:
        type(self).opened_ports.append(str(kwargs.get("port", "")))
        raise AssertionError("virtual scoring tests must not open serial")


def load_virtual_plan() -> dict:
    return load_plan(FIXTURE_PATH)


def send_yaml_command(
    transport: VirtualTransport,
    plan: dict,
    command: str,
    events: list[TuningEvent],
):
    return send_command(
        transport,
        command,
        plan["transport"]["line_ending"],
        plan["commands"]["ok_pattern"],
        plan["commands"].get("error_patterns", []),
        int(plan["transport"]["read_timeout_ms"]),
        require_ok=True,
        event_handler=events.append,
    )


def collect_score(transport: VirtualTransport, plan: dict, events: list[TuningEvent]) -> float:
    telemetry = collect_telemetry(
        transport,
        plan,
        int(plan["scoring"]["trial_window_ms"]),
        events.append,
    )
    score, failures = score_window(plan, telemetry)
    if failures:
        raise AssertionError(f"unexpected virtual telemetry failures: {failures}")
    assert score is not None
    return score


def stable_summary(summary: dict) -> dict:
    normalized = dict(summary)
    normalized["log_path"] = "<session-log>"
    return normalized


class TuningSessionVirtualScoringTests(unittest.TestCase):
    def setUp(self) -> None:
        SerialShouldNotOpen.opened_ports = []

    def test_virtual_scores_improve_as_parameters_approach_fixture_targets(self) -> None:
        plan = load_virtual_plan()
        events: list[TuningEvent] = []
        transport = VirtualTransport(plan).open()

        start = send_yaml_command(transport, plan, plan["commands"]["telemetry_on"], events)
        self.assertTrue(start.ok)
        baseline_score = collect_score(transport, plan, events)

        send_yaml_command(transport, plan, format_template(plan["commands"]["set"], "kp_x", 1.1), events)
        send_yaml_command(transport, plan, format_template(plan["commands"]["set"], "kd_x", 0.25), events)
        target_score = collect_score(transport, plan, events)

        send_yaml_command(transport, plan, format_template(plan["commands"]["set"], "kp_x", 1.35), events)
        away_score = collect_score(transport, plan, events)

        self.assertLess(target_score, baseline_score)
        self.assertGreater(away_score, target_score)
        self.assertEqual(transport.target_parameters["kp_x"], 1.1)
        self.assertEqual(transport.target_parameters["kd_x"], 0.25)

    def test_virtual_fixture_run_is_deterministic_and_exercises_decisions(self) -> None:
        first = self.run_virtual_fixture()
        second = self.run_virtual_fixture()

        first_summary, first_decisions, first_transport = first
        second_summary, second_decisions, second_transport = second
        counts = Counter(decision["decision"] for decision in first_decisions)

        self.assertEqual(first_summary, second_summary)
        self.assertEqual(first_decisions, second_decisions)
        self.assertEqual(counts, {"accept": 4, "hold": 2, "rollback": 2})
        self.assertEqual(first_summary["accepted"], 4)
        self.assertEqual(first_summary["held"], 2)
        self.assertEqual(first_summary["failed"], 2)
        self.assertEqual(first_summary["rolled_back"], 2)
        self.assertLess(first_summary["final_score"], first_summary["baseline_score"])
        self.assertEqual(SerialShouldNotOpen.opened_ports, [])
        self.assertTrue(first_transport.closed)
        self.assertTrue(second_transport.closed)

    def run_virtual_fixture(self) -> tuple[dict, list[dict], VirtualTransport]:
        plan = load_virtual_plan()
        events: list[TuningEvent] = []
        transport = VirtualTransport(plan)

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(tuning_session.serial, "Serial", SerialShouldNotOpen):
                session = TuningSession(
                    plan,
                    FIXTURE_PATH,
                    log_dir=Path(tmpdir),
                    event_handler=events.append,
                    transport_factory=lambda _config: transport,
                )
                exit_code = session.run()

        self.assertEqual(exit_code, 0)
        self.assertEqual(SerialShouldNotOpen.opened_ports, [])
        decisions = [
            {
                "key": event.data["key"],
                "decision": event.data["decision"],
                "score": event.data["score"],
                "improvement": event.data["improvement"],
            }
            for event in events
            if event.type == "decision"
        ]
        return stable_summary(session.summary()), decisions, transport


if __name__ == "__main__":
    unittest.main()
