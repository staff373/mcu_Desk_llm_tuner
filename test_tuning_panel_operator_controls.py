#!/usr/bin/env python
"""No-hardware tests for GUI operator controls."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
import unittest
from unittest.mock import patch

import tkinter as tk

import tuning_session
from tuning_panel import TuningPanel


class SerialShouldNotOpen:
    opened_ports: list[str] = []

    def __init__(self, **kwargs: Any) -> None:
        type(self).opened_ports.append(str(kwargs.get("port", "")))
        raise AssertionError("GUI operator-control tests must not open a real serial port")


class FakeWorker:
    def __init__(self, alive: bool) -> None:
        self.alive = alive

    def is_alive(self) -> bool:
        return self.alive


class FakeSession:
    def __init__(self) -> None:
        self.state = "tuning"
        self.current_parameter_key: str | None = "kp_x"
        self.baseline_parameters: dict[str, float] | None = {"kp_x": 350.0}
        self.calls: list[tuple[str, Any]] = []

    def _result(self, action: str, state: str | None = None) -> SimpleNamespace:
        if state is not None:
            self.state = state
        return SimpleNamespace(ok=True, action=action, state=self.state, message=f"{action} ok", data={})

    def pause(self) -> SimpleNamespace:
        self.calls.append(("pause", None))
        return self._result("pause", "paused")

    def resume(self) -> SimpleNamespace:
        self.calls.append(("resume", None))
        return self._result("resume", "tuning")

    def skip_current_param(self, reason: str = "operator_skip") -> SimpleNamespace:
        self.calls.append(("skip_current_param", reason))
        return self._result("skip_current_param")

    def rollback_to(self, target: str) -> SimpleNamespace:
        self.calls.append(("rollback_to", target))
        return self._result("rollback_to")

    def request_stop(self) -> SimpleNamespace:
        self.calls.append(("request_stop", None))
        return self._result("stop", "stopping")

    def emergency_stop(self) -> SimpleNamespace:
        self.calls.append(("emergency_stop", None))
        return self._result("emergency_stop", "stopping")


def button_enabled(app: TuningPanel, name: str) -> bool:
    return not app.operator_buttons[name].instate(["disabled"])


class TuningPanelOperatorControlTests(unittest.TestCase):
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

    def test_start_buttons_select_monitor_and_auto_tune_modes(self) -> None:
        with patch.object(tuning_session.serial, "Serial", SerialShouldNotOpen):
            app = self._create_panel()
            try:
                selected_modes: list[str] = []

                def fake_start() -> None:
                    selected_modes.append(app.mode.get())

                app.start = fake_start  # type: ignore[method-assign]
                app.operator_buttons["start_monitor"].invoke()
                app.operator_buttons["start_auto_tune"].invoke()

                self.assertEqual(selected_modes, ["monitor", "run"])
            finally:
                app.destroy()

        self.assertEqual(SerialShouldNotOpen.opened_ports, [])

    def test_operator_buttons_are_enabled_from_session_state(self) -> None:
        with patch.object(tuning_session.serial, "Serial", SerialShouldNotOpen):
            app = self._create_panel()
            try:
                session = FakeSession()
                app.session = session
                app.session_worker = FakeWorker(True)  # type: ignore[assignment]
                app._refresh_operator_controls()

                self.assertFalse(button_enabled(app, "start_monitor"))
                self.assertFalse(button_enabled(app, "start_auto_tune"))
                self.assertTrue(button_enabled(app, "pause"))
                self.assertFalse(button_enabled(app, "resume"))
                self.assertTrue(button_enabled(app, "skip_current_param"))
                self.assertTrue(button_enabled(app, "rollback_last_stable"))
                self.assertTrue(button_enabled(app, "rollback_baseline"))
                self.assertTrue(button_enabled(app, "stop"))
                self.assertTrue(button_enabled(app, "emergency_stop"))

                session.state = "paused"
                session.current_parameter_key = None
                app._refresh_operator_controls()

                self.assertFalse(button_enabled(app, "pause"))
                self.assertTrue(button_enabled(app, "resume"))
                self.assertFalse(button_enabled(app, "skip_current_param"))

                session.state = "stopped"
                app.session_worker = FakeWorker(False)  # type: ignore[assignment]
                app._refresh_operator_controls()

                self.assertTrue(button_enabled(app, "start_monitor"))
                self.assertTrue(button_enabled(app, "start_auto_tune"))
                self.assertFalse(button_enabled(app, "stop"))
                self.assertFalse(button_enabled(app, "emergency_stop"))
            finally:
                app.destroy()

        self.assertEqual(SerialShouldNotOpen.opened_ports, [])

    def test_operator_buttons_dispatch_to_session_control_api(self) -> None:
        with patch.object(tuning_session.serial, "Serial", SerialShouldNotOpen):
            app = self._create_panel()
            try:
                session = FakeSession()
                app.session = session
                app.session_worker = FakeWorker(True)  # type: ignore[assignment]

                app._refresh_operator_controls()
                app.operator_buttons["pause"].invoke()

                session.state = "paused"
                app._refresh_operator_controls()
                app.operator_buttons["resume"].invoke()

                session.state = "tuning"
                session.current_parameter_key = "kp_x"
                app._refresh_operator_controls()
                app.operator_buttons["skip_current_param"].invoke()
                app.operator_buttons["rollback_last_stable"].invoke()
                app.operator_buttons["rollback_baseline"].invoke()
                app.operator_buttons["stop"].invoke()
                app.operator_buttons["emergency_stop"].invoke()

                self.assertIn(("pause", None), session.calls)
                self.assertIn(("resume", None), session.calls)
                self.assertIn(("skip_current_param", "gui_operator"), session.calls)
                self.assertIn(("rollback_to", "last_stable"), session.calls)
                self.assertIn(("rollback_to", "baseline"), session.calls)
                self.assertIn(("request_stop", None), session.calls)
                self.assertIn(("emergency_stop", None), session.calls)
            finally:
                app.destroy()

        self.assertEqual(SerialShouldNotOpen.opened_ports, [])


if __name__ == "__main__":
    unittest.main()
