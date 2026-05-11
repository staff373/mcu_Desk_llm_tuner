#!/usr/bin/env python
"""No-hardware tests for the GUI parameter page."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from typing import Any
import queue
import unittest
from unittest.mock import patch

import tkinter as tk

import tuning_session
from tuning_panel import SessionWorker, TuningPanel
from tuning_session import TuningEvent, VirtualTransport
from validate_plan import load_plan


FIXTURE_PATH = Path("fixtures/virtual_mcu_tuning_plan.yaml")


class SerialShouldNotOpen:
    opened_ports: list[str] = []

    def __init__(self, **kwargs: Any) -> None:
        type(self).opened_ports.append(str(kwargs.get("port", "")))
        raise AssertionError("GUI parameter-page tests must not open a real serial port")


def parameter_rows(app: TuningPanel) -> dict[str, list[str]]:
    rows: dict[str, list[str]] = {}
    for item_id in app.parameter_tree.get_children():
        values = [str(value) for value in app.parameter_tree.item(item_id, "values")]
        rows[values[0]] = values
    return rows


class TuningPanelParameterPageTests(unittest.TestCase):
    def setUp(self) -> None:
        SerialShouldNotOpen.opened_ports = []

    def _create_panel(self) -> TuningPanel:
        try:
            app = TuningPanel(run_backend="session")
        except tk.TclError as exc:
            self.skipTest(f"Tk hidden initialization unavailable: {exc}")
        app.withdraw()
        app.update_idletasks()
        return app

    def _load_virtual_plan(self, app: TuningPanel) -> None:
        app.mode.set("run")
        app.plan_path.set(str(FIXTURE_PATH.resolve()))
        self.assertTrue(app.validate_current_plan())
        app.update_idletasks()

    def test_virtual_plan_populates_parameter_page_without_serial(self) -> None:
        with patch.object(tuning_session.serial, "Serial", SerialShouldNotOpen):
            app = self._create_panel()
            try:
                self._load_virtual_plan(app)

                tab_labels = [app.notebook.tab(tab_id, "text") for tab_id in app.notebook.tabs()]
                rows = parameter_rows(app)

                self.assertIn("参数", tab_labels)
                self.assertEqual(set(rows), {"kp_x", "kd_x", "kp_y", "kd_y"})
                self.assertEqual(rows["kp_x"][1], "1.0")
                self.assertEqual(rows["kp_x"][2], "-")
                self.assertEqual(rows["kp_x"][3], "1.0")
                self.assertEqual(rows["kp_x"][4], "-")
                self.assertEqual(rows["kp_x"][5], "0.0..3.0")
                self.assertIn("initial=0.1", rows["kp_x"][6])
                self.assertEqual(rows["kp_x"][7], "参与")
                self.assertEqual(tuple(app.parameter_axis_combo.cget("values")), ("x", "y"))
                self.assertEqual(tuple(app.parameter_group_combo.cget("values")), ("x_axis", "y_axis"))
            finally:
                app.destroy()

        self.assertEqual(SerialShouldNotOpen.opened_ports, [])

    def test_parameter_selection_filters_axis_group_and_specific_keys_without_mutating_plan(self) -> None:
        with patch.object(tuning_session.serial, "Serial", SerialShouldNotOpen):
            app = self._create_panel()
            try:
                self._load_virtual_plan(app)
                original_plan = deepcopy(app.plan)

                app.parameter_axis.set("x")
                self.assertTrue(app.apply_axis_parameter_selection())
                self.assertEqual(app.parameter_active_keys, ["kp_x", "kd_x"])
                rows = parameter_rows(app)
                self.assertEqual(rows["kp_x"][7], "参与")
                self.assertEqual(rows["kp_y"][7], "未参与")

                app.parameter_group.set("y_axis")
                self.assertTrue(app.apply_group_parameter_selection())
                self.assertEqual(app.parameter_active_keys, ["kp_y", "kd_y"])

                app.parameter_keys_text.set("kp_x, kd_y")
                self.assertTrue(app.apply_specific_parameter_selection())
                self.assertEqual(app.parameter_active_keys, ["kp_x", "kd_y"])

                app.parameter_keys_text.set("kp_x, unknown_key")
                self.assertFalse(app.apply_specific_parameter_selection())
                self.assertEqual(app.parameter_active_keys, ["kp_x", "kd_y"])
                self.assertEqual(app.plan, original_plan)
            finally:
                app.destroy()

        self.assertEqual(SerialShouldNotOpen.opened_ports, [])

    def test_runtime_parameter_values_update_from_structured_events(self) -> None:
        with patch.object(tuning_session.serial, "Serial", SerialShouldNotOpen):
            app = self._create_panel()
            try:
                self._load_virtual_plan(app)
                app.session = SimpleNamespace(
                    current={"kp_x": 1.1, "kd_x": 0.2, "kp_y": 1.1, "kd_y": 0.25},
                    baseline_parameters={"kp_x": 1.0, "kd_x": 0.2, "kp_y": 1.1, "kd_y": 0.25},
                    last_stable={"kp_x": 1.1, "kd_x": 0.2, "kp_y": 1.1, "kd_y": 0.25},
                    active_parameter_keys=["kp_x", "kd_x"],
                    skipped_parameter_keys={"kd_x"},
                    current_parameter_key="kp_x",
                )

                app._handle_session_event(TuningEvent("round", "", {"round": 1, "key": "kp_x", "trial_value": 1.1}))
                rows = parameter_rows(app)

                self.assertEqual(rows["kp_x"][1], "1.1")
                self.assertEqual(rows["kp_x"][2], "1.0")
                self.assertEqual(rows["kp_x"][3], "1.1")
                self.assertEqual(rows["kp_x"][4], "1.1")
                self.assertEqual(rows["kp_x"][7], "当前")
                self.assertEqual(rows["kd_x"][7], "已跳过")
                self.assertEqual(rows["kp_y"][7], "未参与")
            finally:
                app.destroy()

        self.assertEqual(SerialShouldNotOpen.opened_ports, [])

    def test_session_worker_accepts_gui_selection_without_mutating_source_plan(self) -> None:
        plan = load_plan(FIXTURE_PATH)
        original_plan = deepcopy(plan)
        items: queue.Queue[object] = queue.Queue()

        def virtual_factory(_transport: dict[str, Any]) -> VirtualTransport:
            return VirtualTransport(plan)

        with patch.object(tuning_session.serial, "Serial", SerialShouldNotOpen):
            worker = SessionWorker(
                plan=plan,
                plan_path=FIXTURE_PATH,
                output_queue=items,
                transport_factory=virtual_factory,
                active_parameter_keys=["kp_x", "kd_x"],
            )

        self.assertEqual(worker.session.active_parameter_keys, ["kp_x", "kd_x"])
        self.assertEqual(plan, original_plan)
        self.assertEqual(SerialShouldNotOpen.opened_ports, [])


if __name__ == "__main__":
    unittest.main()
