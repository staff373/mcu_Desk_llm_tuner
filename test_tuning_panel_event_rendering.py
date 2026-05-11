#!/usr/bin/env python
"""No-hardware tests for structured GUI session event rendering."""

from __future__ import annotations

import unittest
from unittest.mock import patch

import tkinter as tk

import tuning_session
from tuning_panel import TuningPanel
from tuning_session import TuningEvent


class SerialShouldNotOpen:
    opened_ports: list[str] = []

    def __init__(self, **kwargs) -> None:
        type(self).opened_ports.append(str(kwargs.get("port", "")))
        raise AssertionError("GUI event rendering tests must not open a real serial port")


def tree_values_by_label(tree: tk.Widget) -> dict[str, str]:
    rows: dict[str, str] = {}
    for item_id in tree.get_children():  # type: ignore[attr-defined]
        values = tree.item(item_id, "values")  # type: ignore[attr-defined]
        rows[str(tree.item(item_id, "text"))] = str(values[0]) if values else ""
    return rows


def tree_column_values(tree: tk.Widget, index: int) -> list[str]:
    values: list[str] = []
    for item_id in tree.get_children():  # type: ignore[attr-defined]
        row = tree.item(item_id, "values")  # type: ignore[attr-defined]
        if len(row) > index:
            values.append(str(row[index]))
    return values


class TuningPanelEventRenderingTests(unittest.TestCase):
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

    def test_structured_events_route_to_monitor_tuning_and_history_pages(self) -> None:
        with patch.object(tuning_session.serial, "Serial", SerialShouldNotOpen):
            app = self._create_panel()
            try:
                events = [
                    TuningEvent("tx", "SET kp_x 370", {"command": "SET kp_x 370"}),
                    TuningEvent("rx", "OK", {"line": "OK"}),
                    TuningEvent("dat", "", {"sample": {"error_x": 2.5, "pwm_x": 120}}),
                    TuningEvent("baseline", "", {"score": 14.82}),
                    TuningEvent("round", "", {"round": 1, "key": "kp_x", "trial_value": 370}),
                    TuningEvent("decision", "", {"decision": "accept", "score": 11.93, "improvement": 0.195}),
                    TuningEvent("record", "", {"record": {"round": 1, "decision": "accept"}}),
                    TuningEvent("warning", "cooldown short", {"code": "cooldown_short"}),
                    TuningEvent("error", "trial failed", {"error_code": "hard_failure", "category": "hard_failure"}),
                    TuningEvent(
                        "summary",
                        "",
                        {
                            "summary": {
                                "final_score": 11.93,
                                "accepted": 1,
                                "held": 0,
                                "rolled_back": 0,
                                "failed": 1,
                                "stop_reason": "max_rounds",
                            }
                        },
                    ),
                ]
                for event in events:
                    app._handle_session_event(event)
                app.update_idletasks()

                monitor_log = app.log.get("1.0", "end")
                self.assertIn("TX SET kp_x 370", monitor_log)
                self.assertIn("RX OK", monitor_log)
                self.assertIn('DAT {"error_x": 2.5, "pwm_x": 120}', monitor_log)
                self.assertEqual(tree_values_by_label(app.dat_tree), {"error_x": "2.5", "pwm_x": "120"})

                tuning_rows = tree_values_by_label(app.tuning_tree)
                self.assertEqual(tuning_rows["当前轮次"], "1")
                self.assertEqual(tuning_rows["当前评分"], "11.93")
                self.assertEqual(tuning_rows["当前决策"], "接受")
                self.assertEqual(tuning_rows["已接受"], "1")
                self.assertEqual(tuning_rows["失败/错误"], "1")
                self.assertEqual(tuning_rows["停止原因"], "max_rounds")

                history_kinds = tree_column_values(app.history_tree, 1)
                self.assertIn("记录", history_kinds)
                self.assertIn("警告", history_kinds)
                self.assertIn("错误", history_kinds)
                self.assertIn("总结", history_kinds)
            finally:
                app.destroy()

        self.assertEqual(SerialShouldNotOpen.opened_ports, [])

    def test_unknown_session_event_is_recorded_without_crashing(self) -> None:
        with patch.object(tuning_session.serial, "Serial", SerialShouldNotOpen):
            app = self._create_panel()
            try:
                app._handle_session_event(TuningEvent("mystery", "", {"payload": {"value": 7}}))
                app.update_idletasks()

                self.assertIn("mystery", app.log.get("1.0", "end"))
                self.assertIn("事件", tree_column_values(app.history_tree, 1))
            finally:
                app.destroy()

        self.assertEqual(SerialShouldNotOpen.opened_ports, [])


if __name__ == "__main__":
    unittest.main()
