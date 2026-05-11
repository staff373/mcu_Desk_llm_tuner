#!/usr/bin/env python
"""Launch MCU tuning or monitoring in an independent PowerShell window.

The launcher validates the YAML plan before opening a new window. By default it
starts the real tuning executor so the operator can watch full TX/RX/DAT output
in a dedicated console.
"""

from __future__ import annotations

import argparse
import base64
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    from validate_plan import load_plan, validate_plan
except ImportError as exc:  # pragma: no cover - script layout dependent
    print("ERROR: validate_plan.py must be in the same scripts directory.", file=sys.stderr)
    raise SystemExit(3) from exc


def ps_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


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


def default_transcript(plan_path: Path | None, mode: str) -> Path:
    base_dir = plan_path.parent if plan_path is not None else Path(tempfile.gettempdir())
    log_dir = base_dir / "mcu_tuning_logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return log_dir / f"{mode}_window_{stamp}.txt"


def build_powershell_command(
    python_exe: Path,
    target_script: Path,
    plan_path: Path,
    target_args: list[str],
    transcript: Path,
) -> str:
    arg_list = [str(target_script), str(plan_path), *target_args]
    args_literal = "@(" + ", ".join(ps_quote(arg) for arg in arg_list) + ")"
    return "\n".join(
        [
            "$Host.UI.RawUI.WindowTitle = 'MCU Bluetooth Tuning Monitor'",
            f"$py = {ps_quote(str(python_exe))}",
            f"$argsList = {args_literal}",
            f"$transcript = {ps_quote(str(transcript))}",
            "Write-Host 'MCU Bluetooth tuning window started.'",
            "Write-Host ('Transcript: ' + $transcript)",
            "Write-Host ''",
            "& $py @argsList 2>&1 | Tee-Object -FilePath $transcript -Append",
            "$code = $LASTEXITCODE",
            "Write-Host ''",
            "Write-Host ('Process exited with code ' + $code)",
            "Write-Host 'Close this window when you are done reviewing the output.'",
        ]
    )


def build_demo_command(params: list[str], transcript: Path) -> str:
    parsed_params: dict[str, str] = {}
    for item in params:
        if "=" not in item:
            continue
        key, value = item.split("=", 1)
        parsed_params[key.strip()] = value.strip()

    kp_x = parsed_params.get("kp_x", "350")
    kd_x = parsed_params.get("kd_x", "30")
    kp_y = parsed_params.get("kp_y", "320")
    kd_y = parsed_params.get("kd_y", "40")

    lines = [
        "MCU Bluetooth tuning demo window started.",
        "This demo does not open COM ports and does not send real serial commands.",
        "TX STATUS",
        f"RX OK kp_x={kp_x} kd_x={kd_x} kp_y={kp_y} kd_y={kd_y}",
        "TX START",
        "RX OK",
        "RX 1000,320,300,180,20,20,0.1,5,240,250,-160,-10,18,0.1,-6",
        'DAT {"timestamp":1000,"setpoint_x":320,"input_x":300,"pwm_x":180,"error_x":20,"setpoint_y":240,"input_y":250,"pwm_y":-160,"error_y":-10}',
        "RX 1050,320,308,164,12,18,0.1,4,240,246,-138,-6,17,0.1,-5",
        'DAT {"timestamp":1050,"setpoint_x":320,"input_x":308,"pwm_x":164,"error_x":12,"setpoint_y":240,"input_y":246,"pwm_y":-138,"error_y":-6}',
        "Baseline score: 14.82",
        "Round 1: kp_x -> 370",
        "TX SET kp_x 370",
        "RX OK",
        "TX STATUS",
        "RX OK kp_x=370 kd_x=30 kp_y=320 kd_y=40",
        "RX 1200,320,314,145,6,16,0.1,3,240,243,-120,-3,16,0.1,-4",
        'DAT {"timestamp":1200,"setpoint_x":320,"input_x":314,"pwm_x":145,"error_x":6,"setpoint_y":240,"input_y":243,"pwm_y":-120,"error_y":-3}',
        "  accept: score=11.93, improvement=19.50%",
        "Round 2: kd_x -> 35",
        "TX SET kd_x 35",
        "RX OK",
        "TX STATUS",
        "RX OK kp_x=370 kd_x=35 kp_y=320 kd_y=40",
        "RX 1400,320,330,-190,-10,34,0.1,-12,240,242,-118,-2,15,0.1,-4",
        'DAT {"timestamp":1400,"setpoint_x":320,"input_x":330,"pwm_x":-190,"error_x":-10,"setpoint_y":240,"input_y":242,"pwm_y":-118,"error_y":-2}',
        "  rollback: score=16.40, failures=[], rollback=ok",
        "TX SET kd_x 30",
        "RX OK",
        "TX STOP",
        "RX OK",
        'Summary: {"final_parameters":{"kp_x":370,"kd_x":30,"kp_y":320,"kd_y":40},"baseline_score":14.82,"final_score":11.93,"accepted":1,"rolled_back":1,"stop_reason":"demo_complete"}',
    ]
    lines_literal = "@(" + ", ".join(ps_quote(line) for line in lines) + ")"
    return "\n".join(
        [
            "$Host.UI.RawUI.WindowTitle = 'MCU Bluetooth Tuning Demo'",
            f"$transcript = {ps_quote(str(transcript))}",
            "Write-Host ('Transcript: ' + $transcript)",
            "Start-Transcript -Path $transcript -Append | Out-Null",
            f"$lines = {lines_literal}",
            "foreach ($line in $lines) { Write-Host ((Get-Date -Format 'HH:mm:ss.fff') + ' ' + $line); Start-Sleep -Milliseconds 450 }",
            "Stop-Transcript | Out-Null",
            "Write-Host ''",
            "Write-Host 'Demo complete. Close this window when you are done reviewing the output.'",
        ]
    )


def launch_window(command: str) -> subprocess.Popen[Any]:
    encoded = base64.b64encode(command.encode("utf-16le")).decode("ascii")
    return subprocess.Popen(
        [
            "powershell.exe",
            "-NoExit",
            "-ExecutionPolicy",
            "Bypass",
            "-EncodedCommand",
            encoded,
        ],
        creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Open an independent window for MCU tuning TX/RX/DAT output.")
    parser.add_argument("plan", type=Path, nargs="?", help="Path to mcu_tuning_plan.yaml")
    parser.add_argument(
        "--mode",
        choices=["run", "monitor", "demo"],
        default="run",
        help="Window target: run automatic tuning, monitor serial traffic, or show a no-hardware demo. Default: run",
    )
    parser.add_argument(
        "--demo-param",
        action="append",
        default=[],
        help="Demo parameter in key=value form, for example --demo-param kp_x=350",
    )
    parser.add_argument("--transcript", type=Path, default=None, help="Path for window transcript text log")
    parser.add_argument("--no-launch", action="store_true", help="Validate and print the window command without opening it")
    args, target_args = parser.parse_known_args(argv)

    plan_path = args.plan.resolve() if args.plan is not None else None
    if args.mode != "demo":
        if plan_path is None:
            print("ERROR: plan path is required unless --mode demo is used", file=sys.stderr)
            return 2
        plan = load_plan(plan_path)
        validate_or_exit(plan)

    script_dir = Path(__file__).resolve().parent
    if args.mode == "run":
        target_script = script_dir / "run_tuning_plan.py"
    else:
        target_script = script_dir / "monitor_tuning_plan.py"

    target_args = list(target_args)
    if target_args and target_args[0] == "--":
        target_args = target_args[1:]

    transcript = (args.transcript or default_transcript(plan_path, args.mode)).resolve()
    transcript.parent.mkdir(parents=True, exist_ok=True)

    if args.mode == "demo":
        command = build_demo_command(args.demo_param, transcript)
    else:
        if plan_path is None:
            print("ERROR: plan path is required unless --mode demo is used", file=sys.stderr)
            return 2
        command = build_powershell_command(Path(sys.executable), target_script, plan_path, target_args, transcript)
    if args.no_launch:
        print("Window command validated but not launched.")
        print(f"Mode: {args.mode}")
        print(f"Target script: {'built-in demo stream' if args.mode == 'demo' else target_script}")
        print(f"Transcript: {transcript}")
        return 0
    process = launch_window(command)
    print(f"Launched {args.mode} window with PID {process.pid}")
    print(f"Transcript: {transcript}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
