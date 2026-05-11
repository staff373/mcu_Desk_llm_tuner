#!/usr/bin/env python
"""Reusable MCU Bluetooth tuning session core.

This module owns serial I/O, telemetry parsing, scoring, rollback, and
structured events. CLI and GUI frontends should call TuningSession instead of
duplicating execution logic.
"""

from __future__ import annotations

import ast
import json
import math
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

try:
    import serial
except ImportError as exc:  # pragma: no cover - environment dependent
    print("ERROR: pyserial is required. Install with: python -m pip install pyserial")
    raise SystemExit(3) from exc


Number = int | float
EventHandler = Callable[["TuningEvent"], None]


@dataclass
class TuningEvent:
    type: str
    message: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    timestamp: str = field(default_factory=lambda: datetime.now().strftime("%H:%M:%S.%f")[:-3])

    def to_record(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "timestamp": self.timestamp,
            "message": self.message,
            **self.data,
        }


@dataclass
class CommandResult:
    ok: bool
    lines: list[str] = field(default_factory=list)
    error: str | None = None


@dataclass
class TelemetryResult:
    samples: list[dict[str, Any]]
    malformed_count: int
    raw_lines: list[str]


@dataclass
class TrialResult:
    decision: str
    score: float | None
    improvement: float | None
    hard_failures: list[str]
    telemetry: TelemetryResult


class ExecutionError(RuntimeError):
    pass


class TuningSessionState:
    IDLE = "idle"
    VALIDATING = "validating"
    CONNECTED = "connected"
    BASELINE = "baseline"
    TUNING = "tuning"
    PAUSED = "paused"
    ROLLBACK = "rollback"
    STOPPING = "stopping"
    STOPPED = "stopped"
    ERROR = "error"

    VALUES = (
        IDLE,
        VALIDATING,
        CONNECTED,
        BASELINE,
        TUNING,
        PAUSED,
        ROLLBACK,
        STOPPING,
        STOPPED,
        ERROR,
    )


class ConsoleEventRenderer:
    def __init__(self, show_serial: bool = True) -> None:
        self.show_serial = show_serial

    def __call__(self, event: TuningEvent) -> None:
        if event.type in {"tx", "rx", "dat"}:
            if self.show_serial:
                print(f"{event.timestamp} {event.type.upper()} {event.message}")
            return
        if event.type == "warning":
            print(f"WARN: {event.message}")
            return
        if event.type == "summary":
            print("Summary:")
            print(json.dumps(event.data["summary"], ensure_ascii=False, indent=2))
            return
        if event.message:
            print(event.message)


def now_stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def validate_or_exit(plan: Any, validate_plan_func: Callable[[Any], Any]) -> None:
    validator = validate_plan_func(plan)
    if validator.warnings:
        print("WARNINGS:")
        for warning in validator.warnings:
            print(f"  - {warning}")
    if validator.errors:
        print("ERRORS:")
        for error in validator.errors:
            print(f"  - {error}")
        raise SystemExit(1)


def serial_kwargs(transport: dict[str, Any]) -> dict[str, Any]:
    parity_map = {
        "none": serial.PARITY_NONE,
        "even": serial.PARITY_EVEN,
        "odd": serial.PARITY_ODD,
    }
    stopbits_map = {
        1: serial.STOPBITS_ONE,
        1.0: serial.STOPBITS_ONE,
        1.5: serial.STOPBITS_ONE_POINT_FIVE,
        2: serial.STOPBITS_TWO,
        2.0: serial.STOPBITS_TWO,
    }
    bytesize_map = {
        5: serial.FIVEBITS,
        6: serial.SIXBITS,
        7: serial.SEVENBITS,
        8: serial.EIGHTBITS,
    }
    return {
        "port": transport["port"],
        "baudrate": int(transport["baudrate"]),
        "bytesize": bytesize_map[int(transport["data_bits"])],
        "parity": parity_map[str(transport["parity"]).lower()],
        "stopbits": stopbits_map[transport["stop_bits"]],
        "timeout": float(transport["read_timeout_ms"]) / 1000.0,
        "write_timeout": float(transport["write_timeout_ms"]) / 1000.0,
    }


def encode_command(command: str, line_ending: str) -> bytes:
    return f"{command}{line_ending}".encode("utf-8")


def decode_line(raw: bytes) -> str:
    return raw.decode("utf-8", errors="replace").strip()


def emit_event(handler: EventHandler | None, event_type: str, message: str = "", **data: Any) -> None:
    if handler is not None:
        handler(TuningEvent(event_type, message, data))


def send_command(
    ser: serial.Serial,
    command: str,
    line_ending: str,
    ok_pattern: str,
    error_patterns: list[str],
    timeout_ms: int,
    require_ok: bool = True,
    event_handler: EventHandler | None = None,
) -> CommandResult:
    ser.reset_input_buffer()
    ser.write(encode_command(command, line_ending))
    ser.flush()
    emit_event(event_handler, "tx", command, command=command)

    deadline = time.monotonic() + timeout_ms / 1000.0
    lines: list[str] = []
    while time.monotonic() < deadline:
        raw = ser.readline()
        if not raw:
            continue
        line = decode_line(raw)
        if not line:
            continue
        lines.append(line)
        emit_event(event_handler, "rx", line, line=line)
        if any(pattern and pattern in line for pattern in error_patterns):
            return CommandResult(False, lines, f"error response after `{command}`: {line}")
        if ok_pattern and ok_pattern in line:
            return CommandResult(True, lines)

    if require_ok:
        return CommandResult(False, lines, f"OK timeout after `{command}`")
    return CommandResult(True, lines)


def parse_value(raw: str, field_type: str, scale: Number) -> Any:
    field_type = field_type.lower()
    if field_type in {"int", "integer"}:
        return int(float(raw)) * scale
    if field_type in {"float", "double", "number"}:
        return float(raw) * scale
    if field_type in {"bool", "boolean"}:
        normalized = raw.strip().lower()
        return normalized in {"1", "true", "yes", "on"}
    return raw


def parse_telemetry_line(line: str, telemetry: dict[str, Any]) -> dict[str, Any] | None:
    fmt = telemetry["format"]
    if fmt != "csv":
        raise ExecutionError(f"telemetry format `{fmt}` is not implemented by this first executor")

    prefix = telemetry.get("prefix")
    delimiter = telemetry.get("delimiter", ",")
    working = line.strip()
    if prefix:
        if not working.startswith(prefix):
            return None
        working = working[len(prefix) :].strip()
        if working.startswith(delimiter):
            working = working[len(delimiter) :]

    parts = [part.strip() for part in working.split(delimiter)]
    fields = telemetry["fields"]
    if len(parts) != len(fields):
        return None

    sample: dict[str, Any] = {}
    for raw, field_def in zip(parts, fields):
        sample[field_def["name"]] = parse_value(raw, field_def["type"], field_def["scale"])
    return sample


def collect_telemetry(
    ser: serial.Serial,
    plan: dict[str, Any],
    duration_ms: int,
    event_handler: EventHandler | None = None,
) -> TelemetryResult:
    telemetry = plan["telemetry"]
    commands = plan["commands"]
    deadline = time.monotonic() + duration_ms / 1000.0
    samples: list[dict[str, Any]] = []
    raw_lines: list[str] = []
    malformed_count = 0

    while time.monotonic() < deadline:
        raw = ser.readline()
        if not raw:
            continue
        line = decode_line(raw)
        if not line:
            continue
        raw_lines.append(line)
        emit_event(event_handler, "rx", line, line=line)
        if commands.get("ok_pattern") and commands["ok_pattern"] in line:
            continue
        if any(pattern and pattern in line for pattern in commands.get("error_patterns", [])):
            malformed_count += 1
            continue
        sample = parse_telemetry_line(line, telemetry)
        if sample is None:
            malformed_count += 1
            continue
        samples.append(sample)
        emit_event(event_handler, "dat", json.dumps(sample, ensure_ascii=False), sample=sample)

    return TelemetryResult(samples=samples, malformed_count=malformed_count, raw_lines=raw_lines)


def delta(values: list[Number]) -> list[float]:
    if len(values) < 2:
        return [0.0]
    return [float(values[index] - values[index - 1]) for index in range(1, len(values))]


def mean(values: list[Number]) -> float:
    if not values:
        raise ExecutionError("metric has no samples")
    return float(sum(values)) / len(values)


def mean_abs(values: list[Number]) -> float:
    return mean([abs(float(value)) for value in values])


def rms(values: list[Number]) -> float:
    if not values:
        raise ExecutionError("metric has no samples")
    return math.sqrt(sum(float(value) * float(value) for value in values) / len(values))


def max_abs(values: list[Number]) -> float:
    if not values:
        raise ExecutionError("metric has no samples")
    return max(abs(float(value)) for value in values)


def std(values: list[Number]) -> float:
    if not values:
        raise ExecutionError("metric has no samples")
    avg = mean(values)
    return math.sqrt(sum((float(value) - avg) ** 2 for value in values) / len(values))


def count_nonzero(values: list[Any]) -> float:
    return float(sum(1 for value in values if value))


def ratio_nonzero(values: list[Any]) -> float:
    if not values:
        raise ExecutionError("metric has no samples")
    return count_nonzero(values) / len(values)


class SafeEvaluator(ast.NodeVisitor):
    def __init__(self, names: dict[str, Any], functions: dict[str, Callable[..., Any]]) -> None:
        self.names = names
        self.functions = functions

    def visit_Expression(self, node: ast.Expression) -> Any:
        return self.visit(node.body)

    def visit_Constant(self, node: ast.Constant) -> Any:
        if isinstance(node.value, (int, float)):
            return node.value
        raise ExecutionError("formula constants must be numeric")

    def visit_Name(self, node: ast.Name) -> Any:
        if node.id in self.names:
            return self.names[node.id]
        if node.id in self.functions:
            return self.functions[node.id]
        raise ExecutionError(f"unknown formula name: {node.id}")

    def visit_UnaryOp(self, node: ast.UnaryOp) -> Any:
        operand = self.visit(node.operand)
        if isinstance(node.op, ast.USub):
            return -operand
        if isinstance(node.op, ast.UAdd):
            return operand
        raise ExecutionError("unsupported unary operator")

    def visit_BinOp(self, node: ast.BinOp) -> Any:
        left = self.visit(node.left)
        right = self.visit(node.right)
        if isinstance(node.op, ast.Add):
            return left + right
        if isinstance(node.op, ast.Sub):
            return left - right
        if isinstance(node.op, ast.Mult):
            return left * right
        if isinstance(node.op, ast.Div):
            return left / right
        if isinstance(node.op, ast.Pow):
            return left**right
        raise ExecutionError("unsupported binary operator")

    def visit_Call(self, node: ast.Call) -> Any:
        func = self.visit(node.func)
        if func not in self.functions.values():
            raise ExecutionError("formula calls may only use allowed metric functions")
        args = [self.visit(arg) for arg in node.args]
        return func(*args)

    def generic_visit(self, node: ast.AST) -> Any:
        raise ExecutionError(f"unsupported formula syntax: {type(node).__name__}")


def evaluate_formula(formula: str, samples: list[dict[str, Any]]) -> float:
    if not samples:
        raise ExecutionError("no telemetry samples collected")

    field_values: dict[str, list[Any]] = {}
    for key in samples[0]:
        field_values[key] = [sample[key] for sample in samples if key in sample]

    functions: dict[str, Callable[..., Any]] = {
        "delta": delta,
        "mean": mean,
        "mean_abs": mean_abs,
        "rms": rms,
        "max_abs": max_abs,
        "std": std,
        "count_nonzero": count_nonzero,
        "ratio_nonzero": ratio_nonzero,
    }
    tree = ast.parse(formula, mode="eval")
    value = SafeEvaluator(field_values, functions).visit(tree)
    if not isinstance(value, (int, float)):
        raise ExecutionError("formula did not return a numeric score")
    if not math.isfinite(float(value)):
        raise ExecutionError("formula returned non-finite score")
    return float(value)


def improvement_ratio(baseline: float, trial: float, lower_is_better: bool) -> float:
    denominator = abs(baseline) if abs(baseline) > 1e-9 else 1.0
    if lower_is_better:
        return (baseline - trial) / denominator
    return (trial - baseline) / denominator


def format_template(template: str, key: str, value: Number) -> str:
    return template.format(key=key, value=value)


def status_confirms_value(lines: list[str], key: str, value: Number) -> bool:
    numeric_value = float(value)
    escaped_key = re.escape(key)
    pattern = re.compile(rf"\b{escaped_key}\b\s*[:=,\s]\s*([-+]?\d+(?:\.\d+)?)")
    for line in lines:
        match = pattern.search(line)
        if not match:
            continue
        observed = float(match.group(1))
        if math.isclose(observed, numeric_value, rel_tol=1e-4, abs_tol=1e-4):
            return True
    return False


def clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


def build_trial_value(current: float, step: float, direction: int, param: dict[str, Any]) -> float | None:
    minimum = float(param["min"])
    maximum = float(param["max"])
    candidate = clamp(current + direction * step, minimum, maximum)
    if math.isclose(candidate, current, rel_tol=0.0, abs_tol=1e-12):
        opposite = clamp(current - direction * step, minimum, maximum)
        if math.isclose(opposite, current, rel_tol=0.0, abs_tol=1e-12):
            return None
        return opposite
    return candidate


def score_window(plan: dict[str, Any], telemetry: TelemetryResult) -> tuple[float | None, list[str]]:
    hard_failures: list[str] = []
    if not telemetry.samples:
        hard_failures.append("no_telemetry")
        return None, hard_failures
    if telemetry.malformed_count > 0 and plan["telemetry"].get("malformed_policy") == "mark_trial_failed":
        hard_failures.append("malformed_telemetry")
    try:
        score = evaluate_formula(plan["scoring"]["formula"], telemetry.samples)
    except ExecutionError as exc:
        hard_failures.append(f"score_eval_error:{exc}")
        return None, hard_failures
    return score, hard_failures


def log_record(log_path: Path, record: dict[str, Any]) -> None:
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


class TuningSession:
    STATE_IDLE = TuningSessionState.IDLE
    STATE_VALIDATING = TuningSessionState.VALIDATING
    STATE_CONNECTED = TuningSessionState.CONNECTED
    STATE_BASELINE = TuningSessionState.BASELINE
    STATE_TUNING = TuningSessionState.TUNING
    STATE_PAUSED = TuningSessionState.PAUSED
    STATE_ROLLBACK = TuningSessionState.ROLLBACK
    STATE_STOPPING = TuningSessionState.STOPPING
    STATE_STOPPED = TuningSessionState.STOPPED
    STATE_ERROR = TuningSessionState.ERROR
    STATE_VALUES = TuningSessionState.VALUES

    def __init__(
        self,
        plan: dict[str, Any],
        plan_path: Path,
        log_dir: Path | None = None,
        event_handler: EventHandler | None = None,
    ) -> None:
        self.plan = plan
        self.plan_path = plan_path
        self.log_dir = log_dir
        self.event_handler = event_handler
        self.stop_requested = False
        self.pause_requested = False
        self.session_id = f"{self.plan.get('plan_name', 'mcu_tuning')}_{now_stamp()}"
        self.state = self.STATE_IDLE

        self.parameters = {param["key"]: dict(param) for param in plan["parameters"]}
        self.current = {key: float(param["current"]) for key, param in self.parameters.items()}
        self.last_stable = dict(self.current)
        self.steps = {key: float(param["initial_step"]) for key, param in self.parameters.items()}
        self.directions = {key: 1 for key in self.parameters}

        self.accepted = 0
        self.held = 0
        self.failed = 0
        self.rolled_back = 0
        self.stop_reason = "max_rounds"
        self.baseline_score: float | None = None
        self.final_score: float | None = None
        self.log_path: Path | None = None

    def _set_state(self, state: str, reason: str) -> None:
        if state not in self.STATE_VALUES:
            raise ValueError(f"unknown tuning session state: {state}")
        previous_state = self.state
        if previous_state == state:
            return
        self.state = state
        self.emit(
            "state",
            f"State {previous_state} -> {state}",
            previous_state=previous_state,
            next_state=state,
            reason=reason,
            session_id=self.session_id,
        )

    def emit(self, event_type: str, message: str = "", **data: Any) -> None:
        event = TuningEvent(event_type, message, dict(data))
        if event_type == "state":
            event.data.setdefault("timestamp", event.timestamp)
        if self.log_path is not None and event_type == "state":
            log_record(self.log_path, event.to_record())
        if self.event_handler is not None:
            self.event_handler(event)

    def request_stop(self) -> None:
        self.stop_requested = True

    def request_pause(self) -> None:
        self.pause_requested = True

    def resume(self) -> None:
        self.pause_requested = False

    def _wait_if_paused(self) -> None:
        previous_state = self.state
        if self.pause_requested and not self.stop_requested:
            self._set_state(self.STATE_PAUSED, "pause_requested")
        while self.pause_requested and not self.stop_requested:
            time.sleep(0.05)
        if self.state == self.STATE_PAUSED:
            self._set_state(previous_state, "resume_requested")

    def _prepare_log_path(self) -> Path:
        log_dir = self.log_dir
        if log_dir is None:
            log_dir = self.plan_path.parent / "mcu_tuning_logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        self.log_path = log_dir / f"{self.session_id}.jsonl"
        return self.log_path

    def execute_rollback(self, ser: serial.Serial, key: str, value: Number) -> CommandResult:
        commands = self.plan["commands"]
        rollback = self.plan["rollback"]
        transport = self.plan["transport"]
        command = format_template(rollback["command_template"], key, value)
        result = send_command(
            ser,
            command,
            transport["line_ending"],
            commands["ok_pattern"],
            commands.get("error_patterns", []),
            int(transport["read_timeout_ms"]),
            require_ok=True,
            event_handler=self.event_handler,
        )
        if not result.ok:
            return result
        if rollback.get("confirm_with"):
            status = send_command(
                ser,
                rollback["confirm_with"],
                transport["line_ending"],
                commands["ok_pattern"],
                commands.get("error_patterns", []),
                int(transport["read_timeout_ms"]),
                require_ok=True,
                event_handler=self.event_handler,
            )
            if not status.ok:
                return status
            if commands.get("readback_required") and not status_confirms_value(status.lines, key, value):
                return CommandResult(False, status.lines, f"rollback readback mismatch for {key}")
        return result

    def stop_device(self, ser: serial.Serial) -> None:
        commands = self.plan["commands"]
        transport = self.plan["transport"]
        for key in ["telemetry_off", "stop"]:
            command = commands.get(key)
            if not command:
                continue
            result = send_command(
                ser,
                command,
                transport["line_ending"],
                commands["ok_pattern"],
                commands.get("error_patterns", []),
                int(transport["read_timeout_ms"]),
                require_ok=False,
                event_handler=self.event_handler,
            )
            if not result.ok:
                self.emit("warning", f"stop command `{command}` did not complete cleanly: {result.error}")

    def run_trial(
        self,
        ser: serial.Serial,
        param: dict[str, Any],
        trial_value: float,
        baseline_score: float,
    ) -> TrialResult:
        commands = self.plan["commands"]
        transport = self.plan["transport"]
        scoring = self.plan["scoring"]
        step_policy = self.plan["step_policy"]

        set_command = format_template(commands["set"], param["key"], trial_value)
        set_result = send_command(
            ser,
            set_command,
            transport["line_ending"],
            commands["ok_pattern"],
            commands.get("error_patterns", []),
            int(transport["read_timeout_ms"]),
            require_ok=True,
            event_handler=self.event_handler,
        )
        if not set_result.ok:
            return TrialResult("rollback", None, None, ["ok_timeout"], TelemetryResult([], 0, set_result.lines))

        if commands.get("readback_required"):
            status = send_command(
                ser,
                commands["status"],
                transport["line_ending"],
                commands["ok_pattern"],
                commands.get("error_patterns", []),
                int(transport["read_timeout_ms"]),
                require_ok=True,
                event_handler=self.event_handler,
            )
            if not status.ok:
                return TrialResult("rollback", None, None, ["readback_status_failed"], TelemetryResult([], 0, status.lines))
            readback_key = param.get("readback_key") or param["key"]
            if not status_confirms_value(status.lines, readback_key, trial_value):
                return TrialResult("rollback", None, None, ["readback_mismatch"], TelemetryResult([], 0, status.lines))

        time.sleep(float(step_policy["cooldown_ms"]) / 1000.0)
        telemetry = collect_telemetry(ser, self.plan, int(scoring["trial_window_ms"]), self.event_handler)
        score, hard_failures = score_window(self.plan, telemetry)
        if score is None:
            return TrialResult("rollback", None, None, hard_failures, telemetry)

        improvement = improvement_ratio(baseline_score, score, bool(scoring["lower_is_better"]))
        if hard_failures:
            decision = "rollback"
        elif improvement >= float(scoring["accept_if_improvement_gte"]):
            decision = "accept"
        elif improvement <= -float(scoring["rollback_if_degradation_gte"]):
            decision = "rollback"
        else:
            decision = "hold"
        return TrialResult(decision, score, improvement, hard_failures, telemetry)

    def run(self) -> int:
        log_path = self._prepare_log_path()
        self._set_state(self.STATE_VALIDATING, "run_started")
        transport = self.plan["transport"]
        commands = self.plan["commands"]
        scoring = self.plan["scoring"]
        step_policy = self.plan["step_policy"]

        self.emit("info", f"Opening serial port {transport['port']} @ {transport['baudrate']}...")

        with serial.Serial(**serial_kwargs(transport)) as ser:
            self._set_state(self.STATE_CONNECTED, "serial_opened")
            status = send_command(
                ser,
                commands["status"],
                transport["line_ending"],
                commands["ok_pattern"],
                commands.get("error_patterns", []),
                int(transport["read_timeout_ms"]),
                require_ok=True,
                event_handler=self.event_handler,
            )
            if not status.ok:
                self._set_state(self.STATE_ERROR, "initial_status_failed")
                raise ExecutionError(status.error or "initial STATUS failed")

            if commands.get("telemetry_on"):
                start = send_command(
                    ser,
                    commands["telemetry_on"],
                    transport["line_ending"],
                    commands["ok_pattern"],
                    commands.get("error_patterns", []),
                    int(transport["read_timeout_ms"]),
                    require_ok=True,
                    event_handler=self.event_handler,
                )
                if not start.ok:
                    self._set_state(self.STATE_ERROR, "telemetry_on_failed")
                    raise ExecutionError(start.error or "telemetry_on failed")

            try:
                self._set_state(self.STATE_BASELINE, "baseline_started")
                self.emit("info", f"Collecting baseline for {scoring['baseline_window_ms']} ms...")
                baseline_telemetry = collect_telemetry(ser, self.plan, int(scoring["baseline_window_ms"]), self.event_handler)
                self.baseline_score, baseline_failures = score_window(self.plan, baseline_telemetry)
                if self.baseline_score is None:
                    self._set_state(self.STATE_ERROR, "baseline_failed")
                    raise ExecutionError(f"baseline failed: {', '.join(baseline_failures)}")
                self.final_score = self.baseline_score
                self.emit(
                    "baseline",
                    f"Baseline score: {self.baseline_score:.6g}",
                    score=self.baseline_score,
                    telemetry_summary={
                        "samples": len(baseline_telemetry.samples),
                        "malformed_count": baseline_telemetry.malformed_count,
                    },
                )

                self._set_state(self.STATE_TUNING, "baseline_complete")
                parameter_order = step_policy["parameter_order"]
                for round_index in range(1, int(step_policy["max_rounds"]) + 1):
                    self._wait_if_paused()
                    if self.stop_requested:
                        self.stop_reason = "manual_stop"
                        break

                    key = parameter_order[(round_index - 1) % len(parameter_order)]
                    param = self.parameters[key]
                    step = min(self.steps[key], float(param["max_delta_per_round"]))
                    trial_value = build_trial_value(self.current[key], step, self.directions[key], param)
                    if trial_value is None:
                        self.emit("round", f"Round {round_index}: skip {key}, no in-range trial value remains", round=round_index, key=key)
                        continue

                    self.emit("round", f"Round {round_index}: {key} -> {trial_value:g}", round=round_index, key=key, trial_value=trial_value)
                    before_params = dict(self.current)
                    trial = self.run_trial(ser, param, trial_value, self.baseline_score)

                    rollback_status = "not_needed"
                    if trial.decision == "accept":
                        self.accepted += 1
                        self.current[key] = trial_value
                        self.last_stable[key] = trial_value
                        if trial.score is not None:
                            self.final_score = trial.score
                        message = (
                            f"  accept: score={trial.score:.6g}, improvement={trial.improvement:.3%}"
                            if trial.score is not None and trial.improvement is not None
                            else "  accept"
                        )
                        self.emit("decision", message, decision="accept", score=trial.score, improvement=trial.improvement, key=key)
                    elif trial.decision == "hold":
                        self.held += 1
                        previous_state = self.state
                        self._set_state(self.STATE_ROLLBACK, "hold_rollback_started")
                        rollback = self.execute_rollback(ser, key, self.last_stable[key])
                        self._set_state(previous_state, "hold_rollback_finished")
                        rollback_status = "ok" if rollback.ok else f"failed:{rollback.error}"
                        self.steps[key] *= float(step_policy["shrink_on_failure"])
                        self.directions[key] *= -1
                        message = (
                            f"  hold: score={trial.score:.6g}, improvement={trial.improvement:.3%}, rollback={rollback_status}"
                            if trial.score is not None and trial.improvement is not None
                            else f"  hold: rollback={rollback_status}"
                        )
                        self.emit("decision", message, decision="hold", score=trial.score, improvement=trial.improvement, key=key)
                    else:
                        self.failed += 1
                        previous_state = self.state
                        self._set_state(self.STATE_ROLLBACK, "rollback_started")
                        rollback = self.execute_rollback(ser, key, self.last_stable[key])
                        self._set_state(previous_state, "rollback_finished")
                        self.rolled_back += 1
                        rollback_status = "ok" if rollback.ok else f"failed:{rollback.error}"
                        self.steps[key] *= float(step_policy["shrink_on_failure"])
                        self.directions[key] *= -1
                        message = (
                            "  rollback: "
                            f"score={trial.score if trial.score is not None else 'n/a'}, "
                            f"failures={trial.hard_failures}, rollback={rollback_status}"
                        )
                        self.emit(
                            "decision",
                            message,
                            decision="rollback",
                            score=trial.score,
                            improvement=trial.improvement,
                            key=key,
                            hard_failures=trial.hard_failures,
                        )
                        if not rollback.ok:
                            self.stop_reason = "rollback_failed"
                            self._set_state(self.STATE_ERROR, "rollback_failed")
                            break

                    record = {
                        "round": round_index,
                        "changed_parameter": key,
                        "before_params": before_params,
                        "trial_params": {**before_params, key: trial_value},
                        "score_before": self.baseline_score,
                        "score_after": trial.score,
                        "improvement": trial.improvement,
                        "decision": trial.decision,
                        "hard_failures": trial.hard_failures,
                        "telemetry_summary": {
                            "samples": len(trial.telemetry.samples),
                            "malformed_count": trial.telemetry.malformed_count,
                        },
                        "rollback_performed": trial.decision in {"hold", "rollback"},
                        "rollback_status": rollback_status,
                    }
                    log_record(log_path, record)
                    self.emit("record", "", record=record)

                    if trial.hard_failures:
                        self.stop_reason = "hard_failure"
                        break
                else:
                    self.stop_reason = "max_rounds"

            except KeyboardInterrupt:
                self.stop_reason = "user_interrupt"
                self.emit("info", "User interrupt received; stopping and preserving last stable parameters.")
            finally:
                if self.state != self.STATE_ERROR:
                    self._set_state(self.STATE_STOPPING, "cleanup_started")
                self.stop_device(ser)

        summary = self.summary()
        log_record(log_path, {"summary": summary})
        self.emit("summary", "", summary=summary)
        exit_code = 0 if self.stop_reason in {"max_rounds", "user_interrupt", "manual_stop"} else 1
        terminal_state = self.STATE_STOPPED if exit_code == 0 else self.STATE_ERROR
        terminal_reason = "run_completed" if exit_code == 0 else "run_failed"
        self._set_state(terminal_state, terminal_reason)
        return exit_code

    def summary(self) -> dict[str, Any]:
        return {
            "final_parameters": self.current,
            "last_stable": self.last_stable,
            "baseline_score": self.baseline_score,
            "final_score": self.final_score,
            "accepted": self.accepted,
            "held": self.held,
            "failed": self.failed,
            "rolled_back": self.rolled_back,
            "stop_reason": self.stop_reason,
            "log_path": str(self.log_path) if self.log_path is not None else None,
        }


def execute_plan(plan: dict[str, Any], plan_path: Path, log_dir: Path | None, serial_trace: bool = True) -> int:
    session = TuningSession(
        plan=plan,
        plan_path=plan_path,
        log_dir=log_dir,
        event_handler=ConsoleEventRenderer(show_serial=serial_trace),
    )
    return session.run()
