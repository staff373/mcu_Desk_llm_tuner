#!/usr/bin/env python
"""No-hardware tests for the TuningSession transport interface."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import tuning_session
from test_tuning_session_state import make_plan
from tuning_session import PySerialTransport, SessionTransport, TuningSession


class SerialShouldNotOpen:
    opened_ports: list[str] = []

    def __init__(self, **kwargs) -> None:
        type(self).opened_ports.append(str(kwargs.get("port", "")))
        raise AssertionError("transport construction must not open serial")


class DelegateSerial:
    opened_ports: list[str] = []
    last_instance: "DelegateSerial | None" = None

    def __init__(self, **kwargs) -> None:
        self.port = kwargs["port"]
        self.timeout = kwargs["timeout"]
        self.write_timeout = kwargs["write_timeout"]
        self.pending_lines: list[bytes] = []
        self.writes: list[bytes] = []
        self.flushed = False
        self.closed = False
        type(self).opened_ports.append(self.port)
        type(self).last_instance = self

    def reset_input_buffer(self) -> None:
        self.pending_lines.clear()

    def write(self, payload: bytes) -> int:
        self.writes.append(payload)
        self.pending_lines.append(b"OK\n")
        return len(payload)

    def flush(self) -> None:
        self.flushed = True

    def readline(self) -> bytes:
        if self.pending_lines:
            return self.pending_lines.pop(0)
        return b""

    def close(self) -> None:
        self.closed = True


class InjectedTransport:
    timeout = 0.001
    write_timeout = 0.001

    def __init__(self) -> None:
        self.opened = False
        self.closed = False
        self.pending_lines: list[bytes] = []
        self.telemetry_lines: list[bytes] = []
        self.commands: list[str] = []

    def open(self) -> "InjectedTransport":
        self.opened = True
        return self

    def __enter__(self) -> "InjectedTransport":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    def reset_input_buffer(self) -> None:
        self.pending_lines.clear()

    def write(self, payload: bytes) -> int:
        command = payload.decode("utf-8").strip()
        self.commands.append(command)
        self.pending_lines.append(b"OK\n")
        if command == "TEL_ON":
            self.telemetry_lines = [b"DAT,1.0\n"]
        elif command.startswith("SET "):
            self.telemetry_lines = [b"DAT,0.8\n"]
        return len(payload)

    def flush(self) -> None:
        return None

    def readline(self) -> bytes:
        if self.pending_lines:
            return self.pending_lines.pop(0)
        if self.telemetry_lines:
            return self.telemetry_lines.pop(0)
        return b""

    def close(self) -> None:
        self.closed = True


class TuningSessionTransportInterfaceTests(unittest.TestCase):
    def setUp(self) -> None:
        SerialShouldNotOpen.opened_ports = []
        DelegateSerial.opened_ports = []
        DelegateSerial.last_instance = None

    def test_pyserial_transport_construction_does_not_open_real_serial(self) -> None:
        config = make_plan(port="COM8")["transport"]

        with patch.object(tuning_session.serial, "Serial", SerialShouldNotOpen):
            transport = PySerialTransport.from_config(config)

        self.assertIsInstance(transport, SessionTransport)
        self.assertEqual(transport.timeout, 0.001)
        self.assertEqual(transport.write_timeout, 0.001)
        self.assertEqual(SerialShouldNotOpen.opened_ports, [])

    def test_pyserial_transport_delegates_required_interface(self) -> None:
        config = make_plan(port="VIRTUAL-TRANSPORT-INTERFACE")["transport"]

        with patch.object(tuning_session.serial, "Serial", DelegateSerial):
            transport = PySerialTransport.from_config(config)
            opened = transport.open()
            opened.reset_input_buffer()
            bytes_written = opened.write(b"STATUS\n")
            opened.flush()
            line = opened.readline()
            opened.close()

        instance = DelegateSerial.last_instance
        self.assertIsNotNone(instance)
        assert instance is not None
        self.assertEqual(DelegateSerial.opened_ports, ["VIRTUAL-TRANSPORT-INTERFACE"])
        self.assertNotIn("COM8", DelegateSerial.opened_ports)
        self.assertEqual(bytes_written, len(b"STATUS\n"))
        self.assertEqual(line, b"OK\n")
        self.assertEqual(instance.writes, [b"STATUS\n"])
        self.assertTrue(instance.flushed)
        self.assertTrue(instance.closed)

    def test_tuning_session_uses_injected_transport_without_serial_open(self) -> None:
        injected = InjectedTransport()
        plan = make_plan(port="COM8")
        plan["step_policy"]["max_rounds"] = 0

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(tuning_session.serial, "Serial", SerialShouldNotOpen):
                session = TuningSession(
                    plan,
                    Path("virtual_transport_interface.yaml"),
                    log_dir=Path(tmpdir),
                    transport_factory=lambda _config: injected,
                )
                exit_code = session.run()

        self.assertEqual(exit_code, 0)
        self.assertTrue(injected.opened)
        self.assertTrue(injected.closed)
        self.assertEqual(SerialShouldNotOpen.opened_ports, [])
        self.assertEqual(injected.commands, ["STATUS", "TEL_ON", "TEL_OFF", "STOP"])


if __name__ == "__main__":
    unittest.main()
