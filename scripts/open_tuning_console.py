#!/usr/bin/env python
"""Open the local MCU tuning console with a YAML plan preloaded."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PANEL_SCRIPT = PROJECT_ROOT / "tuning_panel.py"


def build_panel_command(
    *,
    python_exe: Path,
    plan_path: Path,
    mode: str,
    ui: str,
    auto_start: bool,
) -> list[str]:
    command = [
        str(python_exe),
        str(PANEL_SCRIPT),
        "--plan",
        str(plan_path),
        "--mode",
        mode,
        "--ui",
        ui,
    ]
    if auto_start:
        command.append("--auto-start")
    return command


def validate_plan_offline(plan_path: Path) -> None:
    sys.path.insert(0, str(PROJECT_ROOT))
    from validate_plan import load_plan, validate_plan  # noqa: PLC0415

    plan = load_plan(plan_path)
    validator = validate_plan(plan)
    if validator.errors:
        errors = "\n".join(f"  - {error}" for error in validator.errors)
        raise SystemExit(f"YAML plan failed offline validation:\n{errors}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Open the local MCU tuning console and preload a YAML plan.")
    parser.add_argument("plan", type=Path, help="Path to mcu_tuning_plan.yaml")
    parser.add_argument("--mode", choices=["demo", "monitor", "run"], default="run")
    parser.add_argument("--ui", choices=["simple", "advanced"], default="simple")
    parser.add_argument("--auto-start", action="store_true", help="Start the selected mode after the console opens")
    parser.add_argument("--no-validate", action="store_true", help="Skip offline YAML validation before launch")
    parser.add_argument("--no-launch", action="store_true", help="Print the launch command without opening the GUI")
    parser.add_argument("--python", type=Path, default=Path(sys.executable), help="Python executable used to open the panel")
    args = parser.parse_args(argv)

    plan_path = args.plan.resolve()
    if not plan_path.exists():
        raise SystemExit(f"YAML plan does not exist: {plan_path}")
    if not PANEL_SCRIPT.exists():
        raise SystemExit(f"tuning_panel.py does not exist: {PANEL_SCRIPT}")
    if not args.no_validate:
        validate_plan_offline(plan_path)

    command = build_panel_command(
        python_exe=args.python.resolve(),
        plan_path=plan_path,
        mode=args.mode,
        ui=args.ui,
        auto_start=args.auto_start,
    )
    if args.no_launch:
        print(" ".join(f'"{part}"' if " " in part else part for part in command))
        return 0

    creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    subprocess.Popen(command, cwd=str(PROJECT_ROOT), creationflags=creationflags)
    print(f"Opened tuning console with plan: {plan_path}")
    print("No serial port is opened unless the operator starts monitor or auto tuning.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
