#!/usr/bin/env python
"""No-hardware tests for opening the local tuning console with a YAML plan."""

from __future__ import annotations

import io
from contextlib import redirect_stdout
from pathlib import Path
import unittest

from scripts.open_tuning_console import PROJECT_ROOT, build_panel_command, main


class OpenTuningConsoleLauncherTests(unittest.TestCase):
    def test_build_panel_command_preloads_plan_without_auto_start_by_default(self) -> None:
        command = build_panel_command(
            python_exe=Path("python"),
            plan_path=Path("mcu_tuning_plan.yaml"),
            mode="run",
            ui="simple",
            auto_start=False,
        )

        self.assertIn(str(PROJECT_ROOT / "tuning_panel.py"), command)
        self.assertIn("--plan", command)
        self.assertIn("mcu_tuning_plan.yaml", command)
        self.assertIn("--mode", command)
        self.assertIn("run", command)
        self.assertNotIn("--auto-start", command)

    def test_no_launch_validates_virtual_fixture_and_prints_command(self) -> None:
        output = io.StringIO()
        plan = PROJECT_ROOT / "fixtures" / "virtual_mcu_tuning_plan.yaml"

        with redirect_stdout(output):
            result = main([str(plan), "--no-launch"])

        self.assertEqual(result, 0)
        rendered = output.getvalue()
        self.assertIn("tuning_panel.py", rendered)
        self.assertIn("--plan", rendered)
        self.assertIn(str(plan), rendered)
        self.assertNotIn("--auto-start", rendered)


if __name__ == "__main__":
    unittest.main()
