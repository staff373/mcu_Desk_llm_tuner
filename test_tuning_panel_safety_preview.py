#!/usr/bin/env python
"""No-hardware tests for the GUI safety and YAML command preview page."""

from __future__ import annotations

from pathlib import Path
from typing import Any
import unittest
from unittest.mock import patch

import tkinter as tk
from tkinter import ttk

import tuning_session
from tuning_panel import TuningPanel
from tuning_session import TuningEvent


FIXTURE_PATH = Path("fixtures/virtual_mcu_tuning_plan.yaml")


class SerialShouldNotOpen:
    opened_ports: list[str] = []

    def __init__(self, **kwargs: Any) -> None:
        type(self).opened_ports.append(str(kwargs.get("port", "")))
        raise AssertionError("GUI safety-preview tests must not open a real serial port")


def safety_rows(app: TuningPanel) -> dict[str, list[str]]:
    rows: dict[str, list[str]] = {}

    def visit(parent: str = "") -> None:
        for item_id in app.safety_tree.get_children(parent):
            values = [str(value) for value in app.safety_tree.item(item_id, "values")]
            rows[str(app.safety_tree.item(item_id, "text"))] = values
            visit(str(item_id))

    visit()
    return rows


def command_rows(app: TuningPanel) -> dict[str, list[str]]:
    rows: dict[str, list[str]] = {}
    for item_id in app.command_preview_tree.get_children():
        values = [str(value) for value in app.command_preview_tree.item(item_id, "values")]
        rows[values[0]] = values
    return rows


def free_text_widgets(widget: tk.Widget) -> list[str]:
    found: list[str] = []
    for child in widget.winfo_children():
        if isinstance(child, (tk.Entry, tk.Text, ttk.Entry)):
            found.append(str(child))
        found.extend(free_text_widgets(child))
    return found


class TuningPanelSafetyPreviewTests(unittest.TestCase):
    def setUp(self) -> None:
        SerialShouldNotOpen.opened_ports = []

    def _create_panel(self) -> TuningPanel:
        try:
            app = TuningPanel(run_backend="session", ui_mode="advanced")
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

    def test_safety_page_loads_yaml_limits_rules_and_preview_without_serial(self) -> None:
        with patch.object(tuning_session.serial, "Serial", SerialShouldNotOpen):
            app = self._create_panel()
            try:
                self._load_virtual_plan(app)

                tab_labels = [app.notebook.tab(tab_id, "text") for tab_id in app.notebook.tabs()]
                rows = safety_rows(app)
                preview = command_rows(app)

                self.assertIn("安全", tab_labels)
                self.assertEqual(rows["transport.read_timeout_ms"], ["5", "正常"])
                self.assertEqual(rows["scoring.trial_window_ms"], ["20", "正常"])
                self.assertEqual(rows["step_policy.max_rounds"], ["8", "正常"])
                self.assertEqual(rows["stop_conditions.unsafe_behavior"][1], "正常")
                self.assertEqual(rows["telemetry.lost"][1], "正常")
                self.assertIn("ratio_nonzero(lost) > 0", rows["hard_failure[0]"][0])
                self.assertEqual(preview["下一条 YAML 命令"][1], "commands.status")
                self.assertEqual(preview["下一条 YAML 命令"][2], "STATUS")
                self.assertEqual(free_text_widgets(app.safety_page), [])
            finally:
                app.destroy()

        self.assertEqual(SerialShouldNotOpen.opened_ports, [])

    def test_structured_events_update_command_preview_and_highlight_safety_rules(self) -> None:
        with patch.object(tuning_session.serial, "Serial", SerialShouldNotOpen):
            app = self._create_panel()
            try:
                self._load_virtual_plan(app)

                app._handle_session_event(TuningEvent("state", "", {"previous_state": "validating", "next_state": "connected"}))
                self.assertEqual(command_rows(app)["下一条 YAML 命令"][2], "STATUS")

                app._handle_session_event(TuningEvent("round", "", {"round": 1, "key": "kp_x", "trial_value": 1.1}))
                preview_before_tx = command_rows(app)["下一条 YAML 命令"]
                self.assertEqual(preview_before_tx[1], "commands.set")
                self.assertEqual(preview_before_tx[2], "SET kp_x 1.1")
                self.assertIn("发送前预览", preview_before_tx[3])

                app._handle_session_event(
                    TuningEvent(
                        "dat",
                        "",
                        {"sample": {"error_x": 1.0, "error_y": 1.0, "pwm_x": 0.2, "pwm_y": 0.3, "lost": 1, "sat": 0}},
                    )
                )
                app._handle_session_event(TuningEvent("error", "read timeout", {"category": "ok_timeout", "code": "ok_timeout"}))
                rows = safety_rows(app)
                self.assertEqual(rows["telemetry.lost"][1], "触发")
                self.assertEqual(rows["hard_failure[0]"][1], "触发")
                self.assertEqual(rows["stop_conditions.unsafe_behavior"][1], "触发")
                self.assertEqual(rows["stop_conditions.communication_failure"][1], "触发")

                app._handle_session_event(TuningEvent("tx", "SET kp_x 1.1", {"command": "SET kp_x 1.1"}))
                preview_after_tx = command_rows(app)
                self.assertEqual(preview_after_tx["最近 TX"][1], "commands.set")
                self.assertEqual(preview_after_tx["最近 TX"][2], "SET kp_x 1.1")
                self.assertEqual(preview_after_tx["下一条 YAML 命令"][3], "等待试验遥测，不发送新命令")
            finally:
                app.destroy()

        self.assertEqual(SerialShouldNotOpen.opened_ports, [])


if __name__ == "__main__":
    unittest.main()
