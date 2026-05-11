#!/usr/bin/env python
"""No-hardware tests for the GUI connection management page."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
import unittest
from unittest.mock import patch

import tkinter as tk

import tuning_panel
import tuning_session
from tuning_panel import TuningPanel
from tuning_session import VirtualTransport


FIXTURE_PATH = Path("fixtures/virtual_mcu_tuning_plan.yaml")


class SerialShouldNotOpen:
    opened_ports: list[str] = []

    def __init__(self, **kwargs: Any) -> None:
        type(self).opened_ports.append(str(kwargs.get("port", "")))
        raise AssertionError("GUI connection tests must not open a real serial port")


def tree_values_by_label(tree: tk.Widget) -> dict[str, str]:
    rows: dict[str, str] = {}
    for item_id in tree.get_children():  # type: ignore[attr-defined]
        values = tree.item(item_id, "values")  # type: ignore[attr-defined]
        rows[str(tree.item(item_id, "text"))] = str(values[0]) if values else ""
    return rows


def button_enabled(app: TuningPanel, name: str) -> bool:
    return not app.connection_buttons[name].instate(["disabled"])


class TuningPanelConnectionManagementTests(unittest.TestCase):
    def setUp(self) -> None:
        SerialShouldNotOpen.opened_ports = []

    def _create_panel(self, *, transport_factory: Any | None = None) -> TuningPanel:
        try:
            app = TuningPanel(run_backend="session", session_transport_factory=transport_factory)
        except tk.TclError as exc:
            self.skipTest(f"Tk hidden initialization unavailable: {exc}")
        app.withdraw()
        app.update_idletasks()
        return app

    def test_connection_page_initializes_without_serial_and_has_controls(self) -> None:
        with patch.object(tuning_session.serial, "Serial", SerialShouldNotOpen):
            app = self._create_panel()
            try:
                tab_labels = [app.notebook.tab(tab_id, "text") for tab_id in app.notebook.tabs()]

                self.assertIn("连接", tab_labels)
                self.assertEqual(set(app.connection_buttons), {"detect_ports", "connect", "disconnect"})
                self.assertTrue(button_enabled(app, "detect_ports"))
                self.assertTrue(button_enabled(app, "connect"))
                self.assertFalse(button_enabled(app, "disconnect"))
                self.assertEqual(app.connection_state.get(), "未连接")
            finally:
                app.destroy()

        self.assertEqual(SerialShouldNotOpen.opened_ports, [])

    def test_validated_plan_displays_yaml_transport_metadata_without_serial(self) -> None:
        with patch.object(tuning_session.serial, "Serial", SerialShouldNotOpen):
            app = self._create_panel()
            try:
                app.mode.set("run")
                app.plan_path.set(str(FIXTURE_PATH.resolve()))

                self.assertTrue(app.validate_current_plan())

                rows = tree_values_by_label(app.connection_tree)
                self.assertEqual(app.port.get(), "VIRTUAL_NO_HARDWARE_TEST_ONLY")
                self.assertEqual(app.baudrate.get(), "115200")
                self.assertEqual(rows["port"], "VIRTUAL_NO_HARDWARE_TEST_ONLY")
                self.assertEqual(rows["baudrate"], "115200")
                self.assertEqual(rows["transport_mode"], "真实 pyserial 传输")
                self.assertIn("计划已校验", app.connection_state.get())
            finally:
                app.destroy()

        self.assertEqual(SerialShouldNotOpen.opened_ports, [])

    def test_detect_ports_does_not_auto_connect_or_open_serial(self) -> None:
        fake_ports = [SimpleNamespace(device="COM_TEST", description="Loopback adapter")]
        with patch.object(tuning_session.serial, "Serial", SerialShouldNotOpen):
            with patch.object(tuning_panel.list_ports, "comports", return_value=fake_ports):
                app = self._create_panel()
                try:
                    detected = app.detect_ports_action()

                    self.assertEqual(detected, ["COM_TEST (Loopback adapter)"])
                    self.assertIsNone(app.connection_transport)
                    self.assertEqual(app.connection_state.get(), "已检测，未连接")
                    self.assertIn("COM_TEST", tree_values_by_label(app.connection_tree)["检测端口"])
                finally:
                    app.destroy()

        self.assertEqual(SerialShouldNotOpen.opened_ports, [])

    def test_real_connect_requires_validated_plan(self) -> None:
        with patch.object(tuning_session.serial, "Serial", SerialShouldNotOpen):
            app = self._create_panel()
            try:
                app.mode.set("run")
                app.plan_path.set(str(FIXTURE_PATH.resolve()))

                self.assertFalse(app.connect_transport())
                self.assertIsNone(app.connection_transport)
                self.assertEqual(app.connection_state.get(), "连接拒绝")
                self.assertIn("请先校验", app.connection_detail.get())
            finally:
                app.destroy()

        self.assertEqual(SerialShouldNotOpen.opened_ports, [])

    def test_virtual_connect_and_disconnect_use_validated_plan_without_serial(self) -> None:
        holder: dict[str, Any] = {}

        def virtual_factory(transport_config: dict[str, Any]) -> VirtualTransport:
            holder["config"] = dict(transport_config)
            transport = VirtualTransport(holder["plan"])
            holder["transport"] = transport
            return transport

        with patch.object(tuning_session.serial, "Serial", SerialShouldNotOpen):
            app = self._create_panel(transport_factory=virtual_factory)
            try:
                app.mode.set("run")
                app.plan_path.set(str(FIXTURE_PATH.resolve()))
                self.assertTrue(app.validate_current_plan())
                holder["plan"] = app.plan

                self.assertTrue(app.connect_transport())

                rows = tree_values_by_label(app.connection_tree)
                self.assertEqual(holder["config"]["port"], "VIRTUAL_NO_HARDWARE_TEST_ONLY")
                self.assertEqual(rows["transport_mode"], "注入传输")
                self.assertEqual(app.connection_state.get(), "已连接")
                self.assertTrue(holder["transport"].opened)
                self.assertFalse(holder["transport"].closed)

                self.assertTrue(app.disconnect_transport())
                self.assertEqual(app.connection_state.get(), "已断开")
                self.assertTrue(holder["transport"].closed)
            finally:
                app.destroy()

        self.assertEqual(SerialShouldNotOpen.opened_ports, [])


if __name__ == "__main__":
    unittest.main()
