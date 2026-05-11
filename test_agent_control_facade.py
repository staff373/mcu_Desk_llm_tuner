#!/usr/bin/env python
"""No-hardware tests for the agent control facade."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import tuning_session
from agent_console_probe import AgentControlFacade
from tuning_session import TuningEvent, VirtualTransport


FIXTURE_PATH = Path("fixtures/virtual_mcu_tuning_plan.yaml")


class SerialShouldNotOpen:
    opened_ports: list[str] = []

    def __init__(self, **kwargs) -> None:
        type(self).opened_ports.append(str(kwargs.get("port", "")))
        raise AssertionError("agent facade tests must not open serial")


class AgentControlFacadeTests(unittest.TestCase):
    def setUp(self) -> None:
        SerialShouldNotOpen.opened_ports = []

    def test_facade_rejects_control_actions_before_validation(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            facade = AgentControlFacade(log_dir=Path(tmpdir))

            expected_public_methods = {
                "export_session_report",
                "load_plan",
                "pause",
                "resume",
                "rollback_to",
                "skip_current_param",
                "start_auto_tune",
                "stop",
                "validate_plan",
            }
            public_methods = {
                name
                for name in dir(facade)
                if not name.startswith("_") and callable(getattr(facade, name))
            }
            self.assertEqual(public_methods, expected_public_methods)

            rejected = [
                facade.start_auto_tune(),
                facade.pause(),
                facade.resume(),
                facade.skip_current_param(),
                facade.rollback_to("baseline"),
                facade.stop(),
                facade.export_session_report(Path(tmpdir) / "report.json"),
            ]
            self.assertTrue(all(not result.ok for result in rejected))
            self.assertTrue(all(result.error_code == "plan_not_validated" for result in rejected))

            loaded = facade.load_plan(FIXTURE_PATH)
            self.assertTrue(loaded.ok)
            pause_after_load = facade.pause()
            self.assertFalse(pause_after_load.ok)
            self.assertEqual(pause_after_load.error_code, "plan_not_validated")

    def test_validate_and_stop_use_session_api_without_opening_serial(self) -> None:
        events: list[TuningEvent] = []
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(tuning_session.serial, "Serial", SerialShouldNotOpen):
                facade = AgentControlFacade(log_dir=Path(tmpdir), event_handler=events.append)
                load_result = facade.load_plan(FIXTURE_PATH)
                validate_result = facade.validate_plan()
                stop_result = facade.stop()

        self.assertTrue(load_result.ok)
        self.assertTrue(validate_result.ok)
        self.assertTrue(stop_result.ok)
        self.assertEqual(stop_result.action, "stop")
        self.assertEqual(stop_result.state, "stopped")
        self.assertEqual(SerialShouldNotOpen.opened_ports, [])
        self.assertTrue(any(event.type == "action" and event.data.get("action") == "stop" for event in events))

    def test_virtual_start_forwards_events_and_records_control_results(self) -> None:
        events: list[TuningEvent] = []
        control_results = []
        flags = {"rollback": False, "pause": False, "skip": False, "resume": False}
        holder: dict[str, VirtualTransport] = {}
        facade: AgentControlFacade

        def provider(plan: dict):
            transport = VirtualTransport(plan)
            holder["transport"] = transport
            return lambda _transport: transport

        def on_event(event: TuningEvent) -> None:
            events.append(event)
            if event.type == "baseline" and not flags["rollback"]:
                flags["rollback"] = True
                control_results.append(facade.rollback_to("baseline"))
            if event.type == "round" and not flags["pause"]:
                flags["pause"] = True
                control_results.append(facade.pause())
                flags["skip"] = True
                control_results.append(facade.skip_current_param("agent_test_skip"))
            if (
                event.type == "state"
                and event.data.get("next_state") == "paused"
                and not flags["resume"]
            ):
                flags["resume"] = True
                control_results.append(facade.resume())

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(tuning_session.serial, "Serial", SerialShouldNotOpen):
                facade = AgentControlFacade(
                    log_dir=Path(tmpdir),
                    event_handler=on_event,
                    transport_factory_provider=provider,
                )
                self.assertTrue(facade.load_plan(FIXTURE_PATH).ok)
                self.assertTrue(facade.validate_plan().ok)
                start_result = facade.start_auto_tune()
                report_path = Path(tmpdir) / "agent_report.json"
                report_result = facade.export_session_report(report_path)

            report = json.loads(report_path.read_text(encoding="utf-8"))
            session_log_existed = Path(report["artifact_paths"]["session_log"]).exists()

        self.assertTrue(start_result.ok)
        self.assertEqual(start_result.action, "start_auto_tune")
        self.assertEqual(start_result.state, "stopped")
        self.assertTrue(report_result.ok)
        self.assertEqual(SerialShouldNotOpen.opened_ports, [])
        self.assertTrue(holder["transport"].closed)
        self.assertTrue(all(result.ok for result in control_results))
        self.assertEqual([result.action for result in control_results], ["rollback_to", "pause", "skip_current_param", "resume"])
        self.assertTrue(any(event.type == "tx" for event in events))
        self.assertTrue(any(event.type == "rx" for event in events))
        self.assertTrue(any(event.type == "dat" for event in events))
        self.assertTrue(any(event.type == "action" and event.data.get("action") == "rollback_to" for event in events))
        self.assertGreater(report["event_counts"]["state"], 0)
        self.assertGreater(report["event_counts"]["tx"], 0)
        self.assertIn("final_summary", report)
        self.assertIn("session_log", report["artifact_paths"])
        self.assertTrue(session_log_existed)
        self.assertTrue(any(item["action"] == "start_auto_tune" and item["ok"] for item in report["action_log"]))


if __name__ == "__main__":
    unittest.main()
