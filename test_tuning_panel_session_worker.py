#!/usr/bin/env python
"""No-hardware tests for the Tk panel session worker path."""

from __future__ import annotations

import queue
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import tkinter as tk

import tuning_session
from tuning_panel import SESSION_DONE, SESSION_EVENT, SessionWorker, TuningPanel, select_execution_backend
from validate_plan import load_plan
from tuning_session import VirtualTransport


FIXTURE_PATH = Path("fixtures/virtual_mcu_tuning_plan.yaml")


class SerialShouldNotOpen:
    opened_ports: list[str] = []

    def __init__(self, **kwargs) -> None:
        type(self).opened_ports.append(str(kwargs.get("port", "")))
        raise AssertionError("GUI no-hardware tests must not open a real serial port")


def drain_items(items: queue.Queue[object]) -> list[object]:
    drained: list[object] = []
    while True:
        try:
            drained.append(items.get_nowait())
        except queue.Empty:
            return drained


class TuningPanelSessionWorkerTests(unittest.TestCase):
    def setUp(self) -> None:
        SerialShouldNotOpen.opened_ports = []

    def test_backend_selection_preserves_subprocess_fallback(self) -> None:
        self.assertEqual(select_execution_backend("demo", "session"), "demo")
        self.assertEqual(select_execution_backend("monitor", "session"), "subprocess")
        self.assertEqual(select_execution_backend("run", "session"), "session")
        self.assertEqual(select_execution_backend("run", "subprocess"), "subprocess")

    def test_session_worker_runs_virtual_session_and_queues_events_without_serial(self) -> None:
        plan = load_plan(FIXTURE_PATH)
        items: queue.Queue[object] = queue.Queue()
        holder: dict[str, VirtualTransport] = {}

        def virtual_factory(_transport: dict) -> VirtualTransport:
            transport = VirtualTransport(plan)
            holder["transport"] = transport
            return transport

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(tuning_session.serial, "Serial", SerialShouldNotOpen):
                worker = SessionWorker(
                    plan=plan,
                    plan_path=FIXTURE_PATH,
                    log_dir=Path(tmpdir),
                    output_queue=items,
                    transport_factory=virtual_factory,
                )
                worker.start()
                worker.join(5)

        self.assertFalse(worker.is_alive())
        self.assertEqual(worker.exit_code, 0)
        self.assertEqual(worker.session.state, "stopped")
        self.assertEqual(SerialShouldNotOpen.opened_ports, [])
        self.assertTrue(holder["transport"].closed)

        drained = drain_items(items)
        event_types = [
            item[1].type
            for item in drained
            if isinstance(item, tuple) and item[0] == SESSION_EVENT
        ]
        done_items = [
            item
            for item in drained
            if isinstance(item, tuple) and item[0] == SESSION_DONE
        ]

        self.assertIn("state", event_types)
        self.assertIn("tx", event_types)
        self.assertIn("rx", event_types)
        self.assertIn("dat", event_types)
        self.assertIn("summary", event_types)
        self.assertEqual(done_items[-1][1], 0)

    def test_hidden_panel_initialization_does_not_open_serial(self) -> None:
        with patch.object(tuning_session.serial, "Serial", SerialShouldNotOpen):
            try:
                app = TuningPanel(run_backend="session")
            except tk.TclError as exc:
                self.skipTest(f"Tk hidden initialization unavailable: {exc}")
            try:
                app.withdraw()
                app.update_idletasks()
            finally:
                app.destroy()

        self.assertEqual(SerialShouldNotOpen.opened_ports, [])


if __name__ == "__main__":
    unittest.main()
