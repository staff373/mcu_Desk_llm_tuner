#!/usr/bin/env python
"""No-hardware tests for GUI session replay and session artifacts."""

from __future__ import annotations

import json
import queue
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

import tkinter as tk

import tuning_session
from agent_console_probe import AgentControlFacade
from tuning_panel import SESSION_DONE, SESSION_ERROR, SESSION_EVENT, TuningPanel, load_session_replay
from tuning_session import VirtualTransport


FIXTURE_PATH = Path("fixtures/virtual_mcu_tuning_plan.yaml")


class SerialShouldNotOpen:
    opened_ports: list[str] = []

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        port = kwargs.get("port")
        if port is None and args:
            port = args[0]
        type(self).opened_ports.append(str(port or ""))
        raise AssertionError("GUI replay tests must not open a real serial port")


def tree_values_by_label(tree: tk.Widget) -> dict[str, str]:
    rows: dict[str, str] = {}
    for item_id in tree.get_children():  # type: ignore[attr-defined]
        values = tree.item(item_id, "values")  # type: ignore[attr-defined]
        rows[str(tree.item(item_id, "text"))] = str(values[0]) if values else ""
    return rows


def drain_panel_queue(app: TuningPanel) -> None:
    while True:
        try:
            item = app.output_queue.get_nowait()
        except queue.Empty:
            return
        if not isinstance(item, tuple) or not item:
            continue
        kind = item[0]
        if kind == SESSION_EVENT:
            app._handle_session_event(item[1])
        elif kind == SESSION_ERROR:
            app._handle_session_error(item[1])
        elif kind == SESSION_DONE:
            app._handle_session_done(item[1])


class TuningPanelSessionReplayTests(unittest.TestCase):
    def setUp(self) -> None:
        SerialShouldNotOpen.opened_ports = []

    def _create_panel(self, *, transport_factory=None) -> TuningPanel:
        try:
            app = TuningPanel(run_backend="session", session_transport_factory=transport_factory)
        except tk.TclError as exc:
            self.skipTest(f"Tk hidden initialization unavailable: {exc}")
        app.withdraw()
        app.update_idletasks()
        return app

    def test_direct_session_creates_replay_artifacts_and_loads_manifest(self) -> None:
        holder: dict[str, VirtualTransport] = {}

        def virtual_factory(_transport: dict[str, Any]) -> VirtualTransport:
            assert app.plan is not None
            transport = VirtualTransport(app.plan)
            holder["transport"] = transport
            return transport

        with tempfile.TemporaryDirectory() as tmpdir:
            plan_copy = Path(tmpdir) / "virtual_mcu_tuning_plan.yaml"
            plan_copy.write_text(FIXTURE_PATH.read_text(encoding="utf-8"), encoding="utf-8")

            with patch.object(tuning_session.serial, "Serial", SerialShouldNotOpen):
                app = self._create_panel(transport_factory=virtual_factory)
                try:
                    app.mode.set("run")
                    app.plan_path.set(str(plan_copy))
                    self.assertTrue(app.validate_current_plan())
                    app._start_session_worker()
                    worker = app.session_worker
                    self.assertIsNotNone(worker)
                    worker.join(5)
                    self.assertFalse(worker.is_alive())
                    drain_panel_queue(app)
                    artifacts = dict(app.current_session_artifacts)

                    for key in ("session_log", "transcript", "plan_snapshot", "final_summary", "manifest"):
                        self.assertTrue(Path(artifacts[key]).exists(), key)

                    self.assertTrue(app.load_replay_file(artifacts["manifest"]))
                    rows = tree_values_by_label(app.replay_summary_tree)
                    diff_rows = tree_values_by_label(app.replay_diff_tree)

                    self.assertEqual(rows["session_id"], artifacts["session_id"])
                    self.assertEqual(rows["accepted"], "4")
                    self.assertEqual(rows["rolled_back"], "2")
                    self.assertNotEqual(rows["baseline_score"], "-")
                    self.assertNotEqual(rows["final_score"], "-")
                    self.assertIn("kp_x", diff_rows)
                    self.assertIn("kd_y", diff_rows)
                finally:
                    app.destroy()

        self.assertEqual(SerialShouldNotOpen.opened_ports, [])
        self.assertTrue(holder["transport"].closed)

    def test_agent_report_export_loads_in_replay_view(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)

            with patch.object(tuning_session.serial, "Serial", SerialShouldNotOpen):
                facade = AgentControlFacade(
                    log_dir=root,
                    transport_factory_provider=lambda plan: VirtualTransport.factory(plan),
                    serial_open_attempts=SerialShouldNotOpen.opened_ports,
                )
                self.assertTrue(facade.load_plan(FIXTURE_PATH).ok)
                self.assertTrue(facade.validate_plan().ok)
                self.assertTrue(facade.start_auto_tune().ok)
                report_path = root / "report.json"
                self.assertTrue(facade.export_session_report(report_path).ok)

                app = self._create_panel()
                try:
                    self.assertTrue(app.load_replay_file(report_path))
                    rows = tree_values_by_label(app.replay_summary_tree)
                    self.assertEqual(rows["accepted"], "4")
                    self.assertEqual(rows["rolled_back"], "2")
                    self.assertIn("decision", rows["event_counts"])
                    self.assertEqual(app.replay_data.load_errors, [])
                finally:
                    app.destroy()

        self.assertEqual(SerialShouldNotOpen.opened_ports, [])

    def test_corrupt_or_partial_log_reports_recoverable_load_errors(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            corrupt_log = Path(tmpdir) / "partial.jsonl"
            corrupt_log.write_text(
                '{"type":"state","session_id":"partial-session"}\n'
                "not-json\n",
                encoding="utf-8",
            )

            result = load_session_replay(corrupt_log)
            self.assertTrue(result.recoverable)
            self.assertEqual(result.session_id, "partial-session")
            self.assertGreaterEqual(len(result.load_errors), 2)

            with patch.object(tuning_session.serial, "Serial", SerialShouldNotOpen):
                app = self._create_panel()
                try:
                    self.assertFalse(app.load_replay_file(corrupt_log))
                    self.assertIn("可恢复加载问题", app.replay_status.get())
                    error_rows = tree_values_by_label(app.replay_error_tree)
                    self.assertIn("可恢复加载问题", error_rows)
                finally:
                    app.destroy()

        self.assertEqual(SerialShouldNotOpen.opened_ports, [])


if __name__ == "__main__":
    unittest.main()
