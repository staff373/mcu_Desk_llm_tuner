#!/usr/bin/env python
"""Monitor MCU Bluetooth serial TX/RX using an executor YAML plan.

This tool opens the configured serial port and displays real transmitted
commands and received lines. It never sends SET commands and never changes
tunable parameters.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    import serial
except ImportError as exc:  # pragma: no cover - environment dependent
    print("ERROR: pyserial is required. Install with: python -m pip install pyserial", file=sys.stderr)
    raise SystemExit(3) from exc

try:
    from run_tuning_plan import decode_line, encode_command, parse_telemetry_line, serial_kwargs
    from validate_plan import load_plan, validate_plan
except ImportError as exc:  # pragma: no cover - script layout dependent
    print("ERROR: run_tuning_plan.py and validate_plan.py must be in the same scripts directory.", file=sys.stderr)
    raise SystemExit(3) from exc


def stamp() -> str:
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]


def validate_or_exit(plan: Any) -> None:
    validator = validate_plan(plan)
    if validator.warnings:
        print("WARNINGS:")
        for warning in validator.warnings:
            print(f"  - {warning}")
    if validator.errors:
        print("ERRORS:")
        for error in validator.errors:
            print(f"  - {error}")
        raise SystemExit(1)


def print_rx(line: str, plan: dict[str, Any], show_parsed: bool) -> None:
    print(f"{stamp()} RX {line}")
    if not show_parsed:
        return
    try:
        sample = parse_telemetry_line(line, plan["telemetry"])
    except Exception as exc:  # noqa: BLE001 - display parse failure to the operator
        print(f"{stamp()} PARSE_ERROR {exc}")
        return
    if sample is not None:
        print(f"{stamp()} DAT {json.dumps(sample, ensure_ascii=False)}")


def send_named_command(ser: serial.Serial, plan: dict[str, Any], name: str) -> None:
    command = plan["commands"].get(name)
    if not command:
        print(f"{stamp()} SKIP command `{name}` is not configured")
        return
    ser.write(encode_command(command, plan["transport"]["line_ending"]))
    ser.flush()
    print(f"{stamp()} TX {command}")


def monitor(plan: dict[str, Any], duration_s: float, send_status: bool, start_telemetry: bool, stop_on_exit: bool, show_parsed: bool) -> None:
    print(f"Opening serial port {plan['transport']['port']} @ {plan['transport']['baudrate']} for monitor...")
    with serial.Serial(**serial_kwargs(plan["transport"])) as ser:
        if send_status:
            send_named_command(ser, plan, "status")
        if start_telemetry:
            send_named_command(ser, plan, "telemetry_on")

        deadline = time.monotonic() + duration_s if duration_s > 0 else None
        try:
            while deadline is None or time.monotonic() < deadline:
                raw = ser.readline()
                if not raw:
                    continue
                line = decode_line(raw)
                if line:
                    print_rx(line, plan, show_parsed)
        except KeyboardInterrupt:
            print(f"{stamp()} INFO user interrupt")
        finally:
            if stop_on_exit:
                send_named_command(ser, plan, "telemetry_off")
                send_named_command(ser, plan, "stop")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Monitor real MCU Bluetooth serial TX/RX from a tuning YAML plan.")
    parser.add_argument("plan", type=Path, help="Path to mcu_tuning_plan.yaml")
    parser.add_argument("--duration", type=float, default=30.0, help="Monitor duration in seconds; 0 means until Ctrl+C")
    parser.add_argument("--send-status", action="store_true", help="Send the YAML status command before listening")
    parser.add_argument("--start-telemetry", action="store_true", help="Send the YAML telemetry_on command before listening")
    parser.add_argument("--stop-on-exit", action="store_true", help="Send telemetry_off and stop before closing")
    parser.add_argument("--raw-only", action="store_true", help="Do not print parsed telemetry objects")
    args = parser.parse_args(argv)

    plan = load_plan(args.plan)
    validate_or_exit(plan)
    monitor(
        plan,
        args.duration,
        args.send_status,
        args.start_telemetry,
        args.stop_on_exit,
        not args.raw_only,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
