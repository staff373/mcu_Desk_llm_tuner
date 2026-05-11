#!/usr/bin/env python
"""No-hardware tests for the deterministic VirtualTransport."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import tuning_session
from test_tuning_session_state import make_plan
from tuning_session import TuningEvent, TuningSession, VirtualTransport, collect_telemetry, send_command


class SerialShouldNotOpen:
    opened_ports: list[str] = []

    def __init__(self, **kwargs) -> None:
        type(self).opened_ports.append(str(kwargs.get("port", "")))
        raise AssertionError("virtual transport must not open serial")


def make_virtual_plan(port: str = "COM8") -> dict:
    plan = make_plan(port=port)
    plan["rollback"] = {"command_template": "RB {key} {value}", "confirm_with": ""}
    plan["step_policy"]["max_rounds"] = 1
    plan["step_policy"]["cooldown_ms"] = 1
    plan["scoring"]["baseline_window_ms"] = 1
    plan["scoring"]["trial_window_ms"] = 1
    return plan


def send_yaml_command(
    transport: VirtualTransport,
    plan: dict,
    command: str,
    events: list[TuningEvent],
):
    return send_command(
        transport,
        command,
        plan["transport"]["line_ending"],
        plan["commands"]["ok_pattern"],
        plan["commands"].get("error_patterns", []),
        int(plan["transport"]["read_timeout_ms"]),
        require_ok=True,
        event_handler=events.append,
    )


class TuningSessionVirtualTransportTests(unittest.TestCase):
    def setUp(self) -> None:
        SerialShouldNotOpen.opened_ports = []

    def test_virtual_transport_direct_yaml_commands_emit_rx_and_dat(self) -> None:
        plan = make_virtual_plan(port="COM8")
        events: list[TuningEvent] = []
        transport = VirtualTransport(plan, target_parameters={"kp_x": 1.2}).open()

        status = send_yaml_command(transport, plan, "STATUS", events)
        telemetry_on = send_yaml_command(transport, plan, "TEL_ON", events)
        set_result = send_yaml_command(transport, plan, "SET kp_x 1.2", events)
        telemetry = collect_telemetry(transport, plan, 1, events.append)
        rollback = send_yaml_command(transport, plan, "RB kp_x 1.0", events)
        telemetry_off = send_yaml_command(transport, plan, "TEL_OFF", events)
        stop = send_yaml_command(transport, plan, "STOP", events)

        self.assertTrue(status.ok)
        self.assertIn("kp_x=1", status.lines[0])
        self.assertTrue(telemetry_on.ok)
        self.assertTrue(set_result.ok)
        self.assertGreater(len(telemetry.samples), 0)
        self.assertTrue(rollback.ok)
        self.assertTrue(telemetry_off.ok)
        self.assertTrue(stop.ok)
        self.assertEqual(transport.parameters["kp_x"], 1.0)
        self.assertEqual(
            transport.commands,
            ["STATUS", "TEL_ON", "SET kp_x 1.2", "RB kp_x 1.0", "TEL_OFF", "STOP"],
        )
        self.assertTrue(any(event.type == "rx" and event.data.get("line", "").startswith("OK") for event in events))
        self.assertTrue(any(event.type == "dat" and "error" in event.data.get("sample", {}) for event in events))

    def test_tuning_session_uses_virtual_transport_without_real_serial_open(self) -> None:
        plan = make_virtual_plan(port="COM8")
        events: list[TuningEvent] = []
        virtual_transport = VirtualTransport(plan)

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(tuning_session.serial, "Serial", SerialShouldNotOpen):
                session = TuningSession(
                    plan,
                    Path("virtual_transport.yaml"),
                    log_dir=Path(tmpdir),
                    event_handler=events.append,
                    transport_factory=lambda _config: virtual_transport,
                )
                exit_code = session.run()

        self.assertEqual(exit_code, 0)
        self.assertTrue(virtual_transport.opened)
        self.assertTrue(virtual_transport.closed)
        self.assertEqual(SerialShouldNotOpen.opened_ports, [])
        self.assertIn("STATUS", virtual_transport.commands)
        self.assertIn("TEL_ON", virtual_transport.commands)
        self.assertIn("SET kp_x 1.1", virtual_transport.commands)
        self.assertIn("RB kp_x 1.0", virtual_transport.commands)
        self.assertEqual(virtual_transport.commands[-2:], ["TEL_OFF", "STOP"])
        self.assertTrue(any(event.type == "tx" and event.data.get("command") == "SET kp_x 1.1" for event in events))
        self.assertTrue(any(event.type == "rx" and event.data.get("line", "").startswith("OK") for event in events))
        self.assertTrue(any(event.type == "dat" and "error" in event.data.get("sample", {}) for event in events))
        summary = session.summary()
        self.assertEqual(summary["rolled_back"], 1)
        self.assertEqual(summary["final_parameters"]["kp_x"], 1.0)


if __name__ == "__main__":
    unittest.main()
