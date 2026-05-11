#!/usr/bin/env python
"""Run an MCU Bluetooth tuning plan over a real serial port.

This CLI is intentionally thin. The reusable execution core lives in
tuning_session.py so GUI and agent controllers can subscribe to structured
session events without scraping stdout.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

try:
    from tuning_session import (
        ExecutionError,
        TuningSession,
        decode_line,
        encode_command,
        execute_plan,
        parse_telemetry_line,
        serial,
        serial_kwargs,
        validate_or_exit as _validate_or_exit,
    )
    from validate_plan import load_plan, validate_plan
except ImportError as exc:  # pragma: no cover - script layout dependent
    print("ERROR: tuning_session.py and validate_plan.py must be in the same scripts directory.", file=sys.stderr)
    raise SystemExit(3) from exc


def validate_or_exit(plan: Any) -> None:
    _validate_or_exit(plan, validate_plan)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Execute an MCU Bluetooth tuning plan over a real serial port.")
    parser.add_argument("plan", type=Path, help="Path to mcu_tuning_plan.yaml")
    parser.add_argument("--log-dir", type=Path, default=None, help="Directory for JSONL execution logs")
    parser.add_argument(
        "--quiet-serial",
        action="store_true",
        help="Hide per-line serial TX/RX/DAT trace. Default is to show the full serial trace.",
    )
    args = parser.parse_args(argv)

    plan = load_plan(args.plan)
    validate_or_exit(plan)

    try:
        return execute_plan(plan, args.plan, args.log_dir, serial_trace=not args.quiet_serial)
    except serial.SerialException as exc:
        print(f"ERROR: serial port failure [serial_exception]: {exc}", file=sys.stderr)
        return 2
    except ExecutionError as exc:
        code = getattr(exc, "code", None)
        if code:
            print(f"ERROR: execution failed [{code}]: {exc}", file=sys.stderr)
        else:
            print(f"ERROR: execution failed: {exc}", file=sys.stderr)
        return 2


__all__ = [
    "ExecutionError",
    "TuningSession",
    "decode_line",
    "encode_command",
    "execute_plan",
    "parse_telemetry_line",
    "serial_kwargs",
    "validate_or_exit",
]


if __name__ == "__main__":
    raise SystemExit(main())
