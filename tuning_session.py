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
class ActionResult:
    ok: bool
    action: str
    state: str
    message: str = ""
    error_code: str | None = None
    data: dict[str, Any] = field(default_factory=dict)

    def to_record(self) -> dict[str, Any]:
        record: dict[str, Any] = {
            "ok": self.ok,
            "action": self.action,
            "state": self.state,
            "message": self.message,
            **self.data,
        }
        if self.error_code is not None:
            record["error_code"] = self.error_code
        return record


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


class StopRequested(RuntimeError):
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
        self._paused_return_state: str | None = None
        self.session_id = f"{self.plan.get('plan_name', 'mcu_tuning')}_{now_stamp()}"
        self.state = self.STATE_IDLE
        self.current_parameter_key: str | None = None
        self.current_round_index: int | None = None
        self._active_serial: serial.Serial | None = None
        self.baseline_parameters: dict[str, float] | None = None
        self.skipped_parameter_keys: set[str] = set()
        self.skipped_parameters: list[dict[str, Any]] = []

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
        self.stop_command_results: list[dict[str, Any]] = []
        self.baseline_score: float | None = None
        self.final_score: float | None = None
        self.log_path: Path | None = None

    def _apply_stop_reason(self, reason: str) -> None:
        if reason == "operator_abort" or self.stop_reason == "max_rounds":
            self.stop_reason = reason

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

    def _reject_action(self, action: str, code: str, message: str, **data: Any) -> ActionResult:
        result = ActionResult(
            ok=False,
            action=action,
            state=self.state,
            message=message,
            error_code=code,
            data=dict(data),
        )
        self.emit(
            "action_rejected",
            message,
            action=action,
            code=code,
            error_code=code,
            state=self.state,
            session_id=self.session_id,
            **data,
        )
        return result

    def _record_stop_command_result(
        self,
        name: str,
        command: str | None,
        status: str,
        *,
        error: str | None = None,
        lines: list[str] | None = None,
        reason: str | None = None,
    ) -> None:
        record: dict[str, Any] = {
            "name": name,
            "status": status,
        }
        if command is not None:
            record["command"] = command
        if error:
            record["error"] = error
        if lines:
            record["lines"] = list(lines)
        if reason:
            record["reason"] = reason
        self.stop_command_results.append(record)
        self.emit(
            "action",
            f"Stop command {name}: {status}",
            action="stop_command",
            state=self.state,
            session_id=self.session_id,
            **record,
        )

    def _record_unavailable_stop_commands(self, reason: str) -> None:
        if self.stop_command_results:
            return
        commands = self.plan.get("commands", {})
        for name in ("telemetry_off", "stop"):
            self._record_stop_command_result(
                name,
                commands.get(name),
                "unavailable",
                reason=reason,
            )

    def _finish_without_connection(self, log_path: Path, reason: str) -> int:
        self._apply_stop_reason("manual_stop")
        self._record_unavailable_stop_commands("no_connection")
        if self.state != self.STATE_STOPPED:
            if self.state != self.STATE_STOPPING:
                self._set_state(self.STATE_STOPPING, "stop_requested")
            self._set_state(self.STATE_STOPPED, reason)
        summary = self.summary()
        log_record(log_path, {"summary": summary})
        self.emit("summary", "", summary=summary)
        return 0

    def request_stop(self) -> ActionResult:
        previous_state = self.state
        already_requested = self.stop_requested or self.state in {self.STATE_STOPPING, self.STATE_STOPPED}
        self.stop_requested = True
        self.pause_requested = False
        self._paused_return_state = None
        if previous_state not in {self.STATE_STOPPED, self.STATE_ERROR}:
            self._apply_stop_reason("manual_stop")
        self.emit(
            "action",
            "Stop requested.",
            action="stop",
            state=previous_state,
            session_id=self.session_id,
            already_requested=already_requested,
        )
        if previous_state == self.STATE_IDLE:
            self._set_state(self.STATE_STOPPING, "stop_requested")
            self._record_unavailable_stop_commands("no_connection")
            self._set_state(self.STATE_STOPPED, "stop_completed_without_connection")
        elif previous_state not in {self.STATE_STOPPING, self.STATE_STOPPED, self.STATE_ERROR}:
            self._set_state(self.STATE_STOPPING, "stop_requested")
        return ActionResult(
            ok=True,
            action="stop",
            state=self.state,
            message="Stop requested.",
            data={
                "previous_state": previous_state,
                "already_requested": already_requested,
            },
        )

    def emergency_stop(self) -> ActionResult:
        previous_state = self.state
        already_requested = self.stop_requested or self.state in {self.STATE_STOPPING, self.STATE_STOPPED}
        self.stop_requested = True
        self.pause_requested = False
        self._paused_return_state = None
        if previous_state not in {self.STATE_STOPPED, self.STATE_ERROR}:
            self._apply_stop_reason("operator_abort")
        self.emit(
            "action",
            "Emergency stop requested.",
            action="emergency_stop",
            state=previous_state,
            session_id=self.session_id,
            already_requested=already_requested,
            stop_reason=self.stop_reason,
            emergency=True,
        )
        if previous_state == self.STATE_IDLE:
            self._set_state(self.STATE_STOPPING, "emergency_stop_requested")
            self._record_unavailable_stop_commands("no_connection")
            self._set_state(self.STATE_STOPPED, "emergency_stop_completed_without_connection")
        elif previous_state not in {self.STATE_STOPPING, self.STATE_STOPPED, self.STATE_ERROR}:
            self._set_state(self.STATE_STOPPING, "emergency_stop_requested")
        return ActionResult(
            ok=True,
            action="emergency_stop",
            state=self.state,
            message="Emergency stop requested.",
            data={
                "previous_state": previous_state,
                "already_requested": already_requested,
                "stop_reason": self.stop_reason,
                "emergency": True,
            },
        )

    def request_emergency_stop(self) -> ActionResult:
        return self.emergency_stop()

    def pause(self) -> ActionResult:
        if self.state in {self.STATE_STOPPING, self.STATE_STOPPED, self.STATE_ERROR}:
            return self._reject_action(
                "pause",
                "session_not_pausable",
                f"Cannot pause while session state is {self.state}.",
            )
        if not self.pause_requested:
            self.pause_requested = True
        result = ActionResult(
            ok=True,
            action="pause",
            state=self.state,
            message="Pause requested.",
            data={"pending": self.state != self.STATE_PAUSED},
        )
        self.emit(
            "action",
            result.message,
            action="pause",
            state=self.state,
            session_id=self.session_id,
            pending=result.data["pending"],
        )
        return result

    def request_pause(self) -> ActionResult:
        return self.pause()

    def resume(self) -> ActionResult:
        if self.state != self.STATE_PAUSED:
            return self._reject_action(
                "resume",
                "session_not_paused",
                f"Cannot resume while session state is {self.state}.",
            )
        paused_state = self.state
        resume_state = self._paused_return_state or self.STATE_TUNING
        self.pause_requested = False
        self._paused_return_state = None
        self.emit(
            "action",
            "Resume requested.",
            action="resume",
            state=paused_state,
            next_state=resume_state,
            session_id=self.session_id,
        )
        self._set_state(resume_state, "resume_requested")
        return ActionResult(
            ok=True,
            action="resume",
            state=self.state,
            message="Resume requested.",
            data={"previous_state": paused_state},
        )

    def _skip_reason_for(self, key: str) -> str:
        for record in self.skipped_parameters:
            if record["key"] == key:
                return str(record["reason"])
        return "operator_skip"

    def skip_current_param(self, reason: str = "operator_skip") -> ActionResult:
        if self.current_parameter_key is None:
            return self._reject_action(
                "skip_current_param",
                "no_active_parameter",
                "Cannot skip because no current parameter is active.",
            )
        key = self.current_parameter_key
        already_skipped = key in self.skipped_parameter_keys
        self.skipped_parameter_keys.add(key)
        if not already_skipped:
            record: dict[str, Any] = {
                "key": key,
                "reason": reason,
                "state": self.state,
            }
            if self.current_round_index is not None:
                record["round"] = self.current_round_index
            self.skipped_parameters.append(record)
        result = ActionResult(
            ok=True,
            action="skip_current_param",
            state=self.state,
            message=f"Skip requested for {key}.",
            data={
                "key": key,
                "reason": reason,
                "already_skipped": already_skipped,
            },
        )
        self.emit(
            "action",
            result.message,
            action="skip_current_param",
            state=self.state,
            session_id=self.session_id,
            key=key,
            reason=reason,
            already_skipped=already_skipped,
        )
        return result

    def _rollback_keys_for(self, target_values: dict[str, float]) -> list[str]:
        ordered_keys: list[str] = []
        for key in self.plan["step_policy"].get("parameter_order", []):
            if key in target_values and key not in ordered_keys:
                ordered_keys.append(key)
        for key in self.parameters:
            if key in target_values and key not in ordered_keys:
                ordered_keys.append(key)
        return ordered_keys

    def rollback_to(self, target: str) -> ActionResult:
        if target not in {"last_stable", "baseline"}:
            return self._reject_action(
                "rollback_to",
                "invalid_rollback_target",
                f"Unsupported rollback target: {target}.",
                target=target,
            )
        if self.baseline_parameters is None:
            return self._reject_action(
                "rollback_to",
                "baseline_missing",
                "Cannot roll back before a baseline exists.",
                target=target,
            )
        if self.state in {self.STATE_STOPPING, self.STATE_STOPPED, self.STATE_ERROR}:
            return self._reject_action(
                "rollback_to",
                "session_not_rollbackable",
                f"Cannot roll back while session state is {self.state}.",
                target=target,
            )
        if self._active_serial is None:
            return self._reject_action(
                "rollback_to",
                "connection_unavailable",
                "Cannot roll back because no active session connection is available.",
                target=target,
            )

        target_values = dict(self.last_stable if target == "last_stable" else self.baseline_parameters)
        rollback_template = self.plan["rollback"]["command_template"]
        previous_state = self.state
        command_results: list[dict[str, Any]] = []
        self._set_state(self.STATE_ROLLBACK, f"{target}_rollback_started")

        for key in self._rollback_keys_for(target_values):
            value = target_values[key]
            command = format_template(rollback_template, key, value)
            result = self.execute_rollback(self._active_serial, key, value)
            command_record: dict[str, Any] = {
                "key": key,
                "value": value,
                "command": command,
                "ok": result.ok,
                "lines": list(result.lines),
            }
            if result.error:
                command_record["error"] = result.error
            command_results.append(command_record)
            if not result.ok:
                self.stop_reason = "rollback_failed"
                self.stop_requested = True
                self.pause_requested = False
                self._paused_return_state = None
                self._set_state(self.STATE_ERROR, "operator_rollback_failed")
                message = f"Rollback to {target} failed for {key}: {result.error}"
                data = {
                    "target": target,
                    "previous_state": previous_state,
                    "failed_key": key,
                    "commands": command_results,
                }
                self.emit(
                    "action",
                    message,
                    action="rollback_to",
                    ok=False,
                    state=self.state,
                    session_id=self.session_id,
                    error_code="rollback_failed",
                    **data,
                )
                return ActionResult(
                    ok=False,
                    action="rollback_to",
                    state=self.state,
                    message=message,
                    error_code="rollback_failed",
                    data=data,
                )

        restored_parameters = {key: float(target_values[key]) for key in self._rollback_keys_for(target_values)}
        self.current.update(restored_parameters)
        self._set_state(previous_state, f"{target}_rollback_finished")
        message = f"Rollback to {target} completed."
        data = {
            "target": target,
            "previous_state": previous_state,
            "restored_parameters": restored_parameters,
            "commands": command_results,
        }
        self.emit(
            "action",
            message,
            action="rollback_to",
            ok=True,
            state=self.state,
            session_id=self.session_id,
            **data,
        )
        return ActionResult(
            ok=True,
            action="rollback_to",
            state=self.state,
            message=message,
            data=data,
        )

    def _wait_if_paused(self) -> None:
        if self.pause_requested and not self.stop_requested:
            if self.state != self.STATE_PAUSED:
                self._paused_return_state = self.state
                self._set_state(self.STATE_PAUSED, "pause_requested")
        while self.pause_requested and not self.stop_requested:
            time.sleep(0.05)
        if self.state == self.STATE_PAUSED:
            resume_state = self._paused_return_state or self.STATE_TUNING
            self._paused_return_state = None
            self._set_state(resume_state, "resume_requested")

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
                self._record_stop_command_result(key, None, "unavailable", reason="missing_yaml_command")
                continue
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
            if result.ok:
                self._record_stop_command_result(key, command, "succeeded", lines=result.lines)
            else:
                status = "timed_out" if result.error and "timeout" in result.error.lower() else "failed"
                self._record_stop_command_result(key, command, status, error=result.error, lines=result.lines)
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
        if self.stop_requested and self.state in {self.STATE_STOPPING, self.STATE_STOPPED}:
            return self._finish_without_connection(log_path, "stop_completed_without_connection")

        self._set_state(self.STATE_VALIDATING, "run_started")
        if self.stop_requested:
            return self._finish_without_connection(log_path, "stop_requested_before_connection")

        transport = self.plan["transport"]
        commands = self.plan["commands"]
        scoring = self.plan["scoring"]
        step_policy = self.plan["step_policy"]

        self.emit("info", f"Opening serial port {transport['port']} @ {transport['baudrate']}...")

        with serial.Serial(**serial_kwargs(transport)) as ser:
            self._active_serial = ser
            self._set_state(self.STATE_CONNECTED, "serial_opened")
            if not self.stop_requested:
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

            if not self.stop_requested and commands.get("telemetry_on"):
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
                self._wait_if_paused()
                if self.stop_requested:
                    self._apply_stop_reason("manual_stop")
                    raise StopRequested()

                self._set_state(self.STATE_BASELINE, "baseline_started")
                self._wait_if_paused()
                if self.stop_requested:
                    self._apply_stop_reason("manual_stop")
                    raise StopRequested()

                self.emit("info", f"Collecting baseline for {scoring['baseline_window_ms']} ms...")
                baseline_telemetry = collect_telemetry(ser, self.plan, int(scoring["baseline_window_ms"]), self.event_handler)
                self.baseline_score, baseline_failures = score_window(self.plan, baseline_telemetry)
                if self.baseline_score is None:
                    self._set_state(self.STATE_ERROR, "baseline_failed")
                    raise ExecutionError(f"baseline failed: {', '.join(baseline_failures)}")
                self.final_score = self.baseline_score
                self.baseline_parameters = dict(self.current)
                self.emit(
                    "baseline",
                    f"Baseline score: {self.baseline_score:.6g}",
                    score=self.baseline_score,
                    telemetry_summary={
                        "samples": len(baseline_telemetry.samples),
                        "malformed_count": baseline_telemetry.malformed_count,
                    },
                )
                if self.stop_requested:
                    self._apply_stop_reason("manual_stop")
                    raise StopRequested()

                self._set_state(self.STATE_TUNING, "baseline_complete")
                if self.stop_requested:
                    self._apply_stop_reason("manual_stop")
                    raise StopRequested()
                parameter_order = list(step_policy["parameter_order"])
                for round_index in range(1, int(step_policy["max_rounds"]) + 1):
                    self._wait_if_paused()
                    if self.stop_requested:
                        self._apply_stop_reason("manual_stop")
                        break

                    available_parameter_order = [
                        key for key in parameter_order if key not in self.skipped_parameter_keys
                    ]
                    if not available_parameter_order:
                        self.stop_reason = "all_parameters_skipped"
                        self.emit(
                            "info",
                            "All parameters have been skipped; stopping tuning.",
                            skipped_parameter_keys=sorted(self.skipped_parameter_keys),
                        )
                        break

                    key = available_parameter_order[(round_index - 1) % len(available_parameter_order)]
                    self.current_parameter_key = key
                    self.current_round_index = round_index
                    param = self.parameters[key]
                    step = min(self.steps[key], float(param["max_delta_per_round"]))
                    trial_value = build_trial_value(self.current[key], step, self.directions[key], param)
                    if trial_value is None:
                        self.emit("round", f"Round {round_index}: skip {key}, no in-range trial value remains", round=round_index, key=key)
                        self.current_parameter_key = None
                        self.current_round_index = None
                        continue

                    self.emit("round", f"Round {round_index}: {key} -> {trial_value:g}", round=round_index, key=key, trial_value=trial_value)
                    self._wait_if_paused()
                    if key in self.skipped_parameter_keys:
                        skip_reason = self._skip_reason_for(key)
                        self.current_parameter_key = None
                        self.current_round_index = None
                        self.emit(
                            "round",
                            f"Round {round_index}: skipped {key} ({skip_reason})",
                            round=round_index,
                            key=key,
                            skipped=True,
                            reason=skip_reason,
                        )
                        record = {
                            "round": round_index,
                            "changed_parameter": key,
                            "before_params": dict(self.current),
                            "trial_params": dict(self.current),
                            "decision": "skipped",
                            "skip_reason": skip_reason,
                            "skipped": True,
                        }
                        log_record(log_path, record)
                        self.emit("record", "", record=record)
                        continue
                    if self.stop_requested:
                        self.current_parameter_key = None
                        self.current_round_index = None
                        self._apply_stop_reason("manual_stop")
                        break

                    before_params = dict(self.current)
                    trial = self.run_trial(ser, param, trial_value, self.baseline_score)
                    self.current_parameter_key = None
                    self.current_round_index = None
                    if self.stop_requested:
                        self._apply_stop_reason("manual_stop")
                        break

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
                    if self.stop_requested:
                        self._apply_stop_reason("manual_stop")
                        break
                else:
                    self.stop_reason = "max_rounds"

            except StopRequested:
                self._apply_stop_reason("manual_stop")
            except KeyboardInterrupt:
                self.stop_reason = "user_interrupt"
                self.emit("info", "User interrupt received; stopping and preserving last stable parameters.")
            finally:
                if self.state != self.STATE_ERROR:
                    self._set_state(self.STATE_STOPPING, "cleanup_started")
                self.stop_device(ser)
                self._active_serial = None

        summary = self.summary()
        log_record(log_path, {"summary": summary})
        self.emit("summary", "", summary=summary)
        exit_code = 0 if self.stop_reason in {"max_rounds", "user_interrupt", "manual_stop", "operator_abort", "all_parameters_skipped"} else 1
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
            "skipped_parameters": self.skipped_parameters,
            "skipped_parameter_keys": sorted(self.skipped_parameter_keys),
            "stop_reason": self.stop_reason,
            "stop_command_results": self.stop_command_results,
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
