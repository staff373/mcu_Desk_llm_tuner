#!/usr/bin/env python
"""No-hardware validation for the test-only virtual YAML plan fixture."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from validate_plan import load_plan, validate_plan


FIXTURE_PATH = Path("fixtures/virtual_mcu_tuning_plan.yaml")


class VirtualPlanFixtureTests(unittest.TestCase):
    def test_virtual_fixture_validates_and_is_clearly_test_only(self) -> None:
        plan = load_plan(FIXTURE_PATH)
        validator = validate_plan(plan)

        self.assertEqual(validator.errors, [])
        self.assertEqual(validator.warnings, [])
        self.assertEqual(plan["transport"]["port"], "VIRTUAL_NO_HARDWARE_TEST_ONLY")
        self.assertNotIn("COM8", json.dumps(plan, ensure_ascii=True))
        labels = " ".join(
            [
                plan["plan_name"],
                *plan["source"]["generated_from"],
                *plan["source"]["confirmed_by_user"],
                plan["transport"]["port"],
            ]
        ).upper()
        self.assertIn("TEST-ONLY", labels)
        self.assertIn("VIRTUAL", labels)

    def test_virtual_fixture_declares_required_parameters_and_telemetry(self) -> None:
        plan = load_plan(FIXTURE_PATH)

        parameter_keys = {item["key"] for item in plan["parameters"]}
        telemetry_fields = {item["name"] for item in plan["telemetry"]["fields"]}

        self.assertTrue({"kp_x", "kd_x", "kp_y", "kd_y"}.issubset(parameter_keys))
        self.assertTrue({"error_x", "error_y", "pwm_x", "pwm_y", "lost", "sat"}.issubset(telemetry_fields))


if __name__ == "__main__":
    unittest.main()
