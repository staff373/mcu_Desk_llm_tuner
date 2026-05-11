#!/usr/bin/env python
"""No-hardware tests for agent session report export."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import tuning_session
from agent_console_probe import AgentControlFacade, validate_session_report_schema
from tuning_session import VirtualTransport


FIXTURE_PATH = Path("fixtures/virtual_mcu_tuning_plan.yaml")


class SerialShouldNotOpen:
    opened_ports: list[str] = []

    def __init__(self, *args, **kwargs) -> None:
        port = kwargs.get("port")
        if port is None and args:
            port = args[0]
        type(self).opened_ports.append(str(port or ""))
        raise AssertionError("session report tests must not open serial")


class FailingOpenTransport:
    def open(self):
        raise tuning_session.serial.SerialException("virtual open failure")


class SessionReportExportTests(unittest.TestCase):
    def setUp(self) -> None:
        SerialShouldNotOpen.opened_ports = []

    def assertValidReport(self, report: dict) -> None:
        self.assertEqual(validate_session_report_schema(report), [])
        self.assertEqual(report["schema_version"], 1)
        self.assertEqual(report["report_type"], "agent_session_report")
        for name, raw_path in report["artifact_paths"].items():
            self.assertTrue(report["artifact_exists"][name], name)
            self.assertTrue(Path(raw_path).exists(), name)

    def load_validate(self, facade: AgentControlFacade) -> None:
        self.assertTrue(facade.load_plan(FIXTURE_PATH).ok)
        self.assertTrue(facade.validate_plan().ok)

    def test_report_schema_for_successful_virtual_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)

            def provider(plan: dict):
                return VirtualTransport.factory(plan)

            with patch.object(tuning_session.serial, "Serial", SerialShouldNotOpen):
                facade = AgentControlFacade(
                    log_dir=root,
                    transport_factory_provider=provider,
                    serial_open_attempts=SerialShouldNotOpen.opened_ports,
                )
                self.load_validate(facade)
                start_result = facade.start_auto_tune()
                report_path = root / "successful_report.json"
                export_result = facade.export_session_report(report_path)

            report = json.loads(report_path.read_text(encoding="utf-8"))
            session_log = Path(report["artifact_paths"]["session_log"])
            session_records = [
                json.loads(line)
                for line in session_log.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            self.assertValidReport(report)

        self.assertTrue(start_result.ok)
        self.assertTrue(export_result.ok)
        self.assertEqual(report["state"], "stopped")
        self.assertEqual(report["real_serial_status"], "not opened")
        self.assertIn("did not open COM8", report["serial_safety"]["statement"])
        self.assertEqual(report["serial_safety"]["serial_open_attempts"], [])
        self.assertIn("report", report["artifact_paths"])
        self.assertIn("session_log", report["artifact_paths"])
        self.assertIn("final_summary", report)
        self.assertIsInstance(report["final_summary"], dict)
        self.assertTrue(any(record.get("summary") for record in session_records))
        self.assertTrue(any(item["action"] == "export_session_report" for item in report["action_log"]))
        self.assertEqual(SerialShouldNotOpen.opened_ports, [])

    def test_report_can_be_exported_after_stopped_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            with patch.object(tuning_session.serial, "Serial", SerialShouldNotOpen):
                facade = AgentControlFacade(
                    log_dir=root,
                    transport_factory_provider=lambda plan: VirtualTransport.factory(plan),
                    serial_open_attempts=SerialShouldNotOpen.opened_ports,
                )
                self.load_validate(facade)
                stop_result = facade.stop()
                report_path = root / "stopped_report.json"
                export_result = facade.export_session_report(report_path)

            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertValidReport(report)

        self.assertTrue(stop_result.ok)
        self.assertTrue(export_result.ok)
        self.assertEqual(report["state"], "stopped")
        self.assertIsInstance(report["final_summary"], dict)
        self.assertEqual(report["real_serial_status"], "not opened")
        self.assertNotIn("session_log", report["artifact_paths"])
        self.assertEqual(SerialShouldNotOpen.opened_ports, [])

    def test_report_can_be_exported_after_error_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)

            def provider(_plan: dict):
                return lambda _transport: FailingOpenTransport()

            with patch.object(tuning_session.serial, "Serial", SerialShouldNotOpen):
                facade = AgentControlFacade(
                    log_dir=root,
                    transport_factory_provider=provider,
                    serial_open_attempts=SerialShouldNotOpen.opened_ports,
                )
                self.load_validate(facade)
                start_result = facade.start_auto_tune()
                report_path = root / "error_report.json"
                export_result = facade.export_session_report(report_path)

            report = json.loads(report_path.read_text(encoding="utf-8"))
            session_log = Path(report["artifact_paths"]["session_log"])
            session_records = [
                json.loads(line)
                for line in session_log.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            self.assertValidReport(report)

        self.assertFalse(start_result.ok)
        self.assertEqual(start_result.error_code, "serial_exception")
        self.assertTrue(export_result.ok)
        self.assertEqual(report["state"], "error")
        self.assertEqual(report["last_exit_code"], 2)
        self.assertGreater(report["event_counts"]["error"], 0)
        self.assertIsInstance(report["final_summary"], dict)
        self.assertTrue(any(record.get("type") == "error" for record in session_records))
        self.assertEqual(report["real_serial_status"], "not opened")
        self.assertEqual(SerialShouldNotOpen.opened_ports, [])


if __name__ == "__main__":
    unittest.main()
