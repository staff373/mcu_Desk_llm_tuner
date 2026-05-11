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
from typing import Any, Callable, Protocol, runtime_checkable

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
    category: str | None = None
    code: str | None = None
    recoverable: bool = False


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
    failure_messages: dict[str, str] = field(default_factory=dict)


class ExecutionError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        category: str = "hard_failure",
        code: str = "hard_failure",
        recoverable: bool = False,
        action: str = "run",
        context: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.category = category
        self.code = code
        self.recoverable = recoverable
        self.action = action
        self.context = dict(context or {})


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
        if event.type == "error":
            code = event.data.get("code") or event.data.get("error_code") or "unknown"
            print(f"ERROR [{code}]: {event.message}")
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


@runtime_checkable
class SessionTransport(Protocol):
    @property
    def timeout(self) -> float | None:
        ...

    @timeout.setter
    def timeout(self, value: float | None) -> None:
        ...

    @property
    def write_timeout(self) -> float | None:
        ...

    @write_timeout.setter
    def write_timeout(self, value: float | None) -> None:
        ...

    def open(self) -> "SessionTransport":
        ...

    def reset_input_buffer(self) -> None:
        ...

    def write(self, payload: bytes) -> int:
        ...

    def flush(self) -> None:
        ...

    def readline(self) -> bytes:
        ...

    def close(self) -> None:
        ...

    def __enter__(self) -> "SessionTransport":
        ...

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        ...


TransportFactory = Callable[[dict[str, Any]], SessionTransport]


class PySerialTransport:
    def __init__(self, options: dict[str, Any]) -> None:
        self.options = dict(options)
        self._serial: Any | None = None

    @classmethod
    def from_config(cls, transport: dict[str, Any]) -> "PySerialTransport":
        return cls(serial_kwargs(transport))

    @property
    def timeout(self) -> float | None:
        if self._serial is not None:
            return getattr(self._serial, "timeout", self.options.get("timeout"))
        return self.options.get("timeout")

    @timeout.setter
    def timeout(self, value: float | None) -> None:
        self.options["timeout"] = value
        if self._serial is not None:
            self._serial.timeout = value

    @property
    def write_timeout(self) -> float | None:
        if self._serial is not None:
            return getattr(self._serial, "write_timeout", self.options.get("write_timeout"))
        return self.options.get("write_timeout")

    @write_timeout.setter
    def write_timeout(self, value: float | None) -> None:
        self.options["write_timeout"] = value
        if self._serial is not None:
            self._serial.write_timeout = value

    def open(self) -> SessionTransport:
        if self._serial is None:
            self._serial = serial.Serial(**self.options)
        return self

    def _require_open(self) -> Any:
        if self._serial is None:
            raise serial.SerialException("transport is not open")
        return self._serial

    def reset_input_buffer(self) -> None:
        self._require_open().reset_input_buffer()

    def write(self, payload: bytes) -> int:
        return self._require_open().write(payload)

    def flush(self) -> None:
        self._require_open().flush()

    def readline(self) -> bytes:
        return self._require_open().readline()

    def close(self) -> None:
        if self._serial is not None:
            self._serial.close()
            self._serial = None

    def __enter__(self) -> SessionTransport:
        return self.open()

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()


def _parameter_command_pattern(template: str, parameter_keys: list[str]) -> re.Pattern[str]:
    key_pattern = "|".join(re.escape(key) for key in sorted(parameter_keys, key=len, reverse=True))
    if not key_pattern:
        key_pattern = r"[^\s]+"
    value_pattern = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?"
    pattern = re.escape(template)
    pattern = pattern.replace(r"\{key\}", f"(?P<key>{key_pattern})")
    pattern = pattern.replace(r"\{value\}", f"(?P<value>{value_pattern})")
    return re.compile(f"^{pattern}$")


class VirtualTransport:
    """Deterministic no-hardware transport for tests and probe scripts."""

    def __init__(
        self,
        plan: dict[str, Any],
        *,
        target_parameters: dict[str, Number] | None = None,
        samples_per_window: int = 3,
    ) -> None:
        self.plan = plan
        self.timeout = float(plan["transport"]["read_timeout_ms"]) / 1000.0
        self.write_timeout = float(plan["transport"]["write_timeout_ms"]) / 1000.0
        self.opened = False
        self.closed = False
        self.telemetry_enabled = False
        self.samples_per_window = max(1, int(samples_per_window))
        self.commands: list[str] = []
        self.pending_lines: list[bytes] = []
        self.telemetry_lines: list[bytes] = []
        self.parameters = {
            param["key"]: float(param["current"])
            for param in plan.get("parameters", [])
        }
        self.target_parameters = dict(target_parameters or self.parameters)
        parameter_keys = list(self.parameters)
        self._set_pattern = _parameter_command_pattern(plan["commands"]["set"], parameter_keys)
        self._rollback_pattern = _parameter_command_pattern(plan["rollback"]["command_template"], parameter_keys)

    @classmethod
    def factory(
        cls,
        plan: dict[str, Any],
        **kwargs: Any,
    ) -> TransportFactory:
        return lambda _transport: cls(plan, **kwargs)

    def open(self) -> "VirtualTransport":
        self.opened = True
        self.closed = False
        return self

    def reset_input_buffer(self) -> None:
        self.pending_lines.clear()
        self.telemetry_lines.clear()

    def write(self, payload: bytes) -> int:
        command = payload.decode("utf-8", errors="replace").strip()
        self.commands.append(command)
        self._handle_command(command)
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

    def __enter__(self) -> "VirtualTransport":
        return self.open()

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()

    def _handle_command(self, command: str) -> None:
        commands = self.plan["commands"]
        if command == commands["status"]:
            self._queue_ok(self._status_payload())
            if self.telemetry_enabled:
                self._queue_telemetry_window()
            return
        if command == commands["telemetry_on"]:
            self.telemetry_enabled = True
            self._queue_ok()
            self._queue_telemetry_window()
            return
        if command == commands["telemetry_off"]:
            self.telemetry_enabled = False
            self._queue_ok()
            return
        if command == commands["stop"]:
            self._queue_ok()
            return

        if self._apply_parameter_command(command, self._set_pattern):
            self._queue_ok()
            if self.telemetry_enabled:
                self._queue_telemetry_window()
            return
        if self._apply_parameter_command(command, self._rollback_pattern):
            self._queue_ok()
            if self.telemetry_enabled:
                self._queue_telemetry_window()
            return

        error_patterns = commands.get("error_patterns") or ["ERR"]
        self._queue_line(f"{error_patterns[0]} unknown command: {command}")

    def _apply_parameter_command(self, command: str, pattern: re.Pattern[str]) -> bool:
        match = pattern.match(command)
        if not match:
            return False
        key = match.group("key")
        value = float(match.group("value"))
        if key not in self.parameters:
            return False
        self.parameters[key] = value
        return True

    def _queue_ok(self, suffix: str = "") -> None:
        ok_pattern = self.plan["commands"].get("ok_pattern") or "OK"
        line = ok_pattern if not suffix else f"{ok_pattern} {suffix}"
        self._queue_line(line)

    def _queue_line(self, line: str) -> None:
        self.pending_lines.append(f"{line}\n".encode("utf-8"))

    def _status_payload(self) -> str:
        return " ".join(f"{key}={value:g}" for key, value in self.parameters.items())

    def _queue_telemetry_window(self) -> None:
        if self.plan["telemetry"]["format"] != "csv":
            return
        for sample_index in range(self.samples_per_window):
            self.telemetry_lines.append(self._format_telemetry_line(sample_index))

    def _format_telemetry_line(self, sample_index: int) -> bytes:
        telemetry = self.plan["telemetry"]
        delimiter = telemetry.get("delimiter", ",")
        values = [
            self._format_telemetry_value(
                self._telemetry_value(str(field["name"]), sample_index),
                str(field.get("type", "float")),
            )
            for field in telemetry["fields"]
        ]
        body = delimiter.join(values)
        prefix = telemetry.get("prefix")
        line = f"{prefix}{delimiter}{body}" if prefix else body
        return f"{line}\n".encode("utf-8")

    def _telemetry_value(self, field_name: str, sample_index: int) -> Number | bool:
        normalized = field_name.lower()
        if normalized in {"lost", "sat", "saturated"}:
            return False
        if "pwm" in normalized:
            return min(1.0, 0.2 + self._axis_error(normalized) * 10.0)
        if "error" in normalized:
            return self._axis_error(normalized) + sample_index * 0.001
        if normalized in self.parameters:
            return self.parameters[normalized]
        return self._overall_error() + sample_index * 0.001

    def _axis_error(self, field_name: str) -> float:
        suffix = field_name.rsplit("_", 1)[-1] if "_" in field_name else ""
        matching_keys = [
            key
            for key in self.parameters
            if suffix and key.lower().endswith(f"_{suffix}")
        ]
        if not matching_keys:
            return self._overall_error()
        return sum(self._parameter_error(key) for key in matching_keys) / len(matching_keys)

    def _overall_error(self) -> float:
        if not self.parameters:
            return 0.0
        return sum(self._parameter_error(key) for key in self.parameters) / len(self.parameters)

    def _parameter_error(self, key: str) -> float:
        target = float(self.target_parameters.get(key, self.parameters[key]))
        return abs(float(self.parameters[key]) - target)

    def _format_telemetry_value(self, value: Number | bool, field_type: str) -> str:
        normalized_type = field_type.lower()
        if normalized_type in {"bool", "boolean"}:
            return "1" if bool(value) else "0"
        if normalized_type in {"int", "integer"}:
            return str(int(round(float(value))))
        return f"{float(value):.6g}"


def encode_command(command: str, line_ending: str) -> bytes:
    return f"{command}{line_ending}".encode("utf-8")


def decode_line(raw: bytes) -> str:
    return raw.decode("utf-8", errors="replace").strip()


def emit_event(handler: EventHandler | None, event_type: str, message: str = "", **data: Any) -> None:
    if handler is not None:
        handler(TuningEvent(event_type, message, data))


def send_command(
    ser: SessionTransport,
    command: str,
    line_ending: str,
    ok_pattern: str,
    error_patterns: list[str],
    timeout_ms: int,
    require_ok: bool = True,
    event_handler: EventHandler | None = None,
) -> CommandResult:
    try:
        ser.reset_input_buffer()
        ser.write(encode_command(command, line_ending))
        ser.flush()
    except serial.SerialException as exc:
        return CommandResult(
            False,
            [],
            f"serial exception during `{command}`: {exc}",
            "serial_exception",
            "serial_exception",
            False,
        )
    emit_event(event_handler, "tx", command, command=command)

    deadline = time.monotonic() + timeout_ms / 1000.0
    lines: list[str] = []
    while time.monotonic() < deadline:
        try:
            raw = ser.readline()
        except serial.SerialException as exc:
            return CommandResult(
                False,
                lines,
                f"serial exception during `{command}`: {exc}",
                "serial_exception",
                "serial_exception",
                False,
            )
        if not raw:
            continue
        line = decode_line(raw)
        if not line:
            continue
        lines.append(line)
        emit_event(event_handler, "rx", line, line=line)
        if any(pattern and pattern in line for pattern in error_patterns):
            return CommandResult(
                False,
                lines,
                f"error response after `{command}`: {line}",
                "hard_failure",
                "command_error_response",
                False,
            )
        if ok_pattern and ok_pattern in line:
            return CommandResult(True, lines)

    if require_ok:
        return CommandResult(
            False,
            lines,
            f"OK timeout after `{command}`",
            "ok_timeout",
            "ok_timeout",
            True,
        )
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
    ser: SessionTransport,
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


def standard_error_fields(failure: str) -> tuple[str, str, bool]:
    if failure == "serial_exception":
        return "serial_exception", "serial_exception", False
    if failure == "ok_timeout":
        return "ok_timeout", "ok_timeout", True
    if failure == "readback_status_failed":
        return "ok_timeout", "readback_status_failed", True
    if failure == "readback_mismatch":
        return "readback_mismatch", "readback_mismatch", True
    if failure == "malformed_telemetry":
        return "malformed_telemetry", "malformed_telemetry", False
    if failure == "rollback_failed":
        return "rollback_failed", "rollback_failed", False
    if failure.startswith("score_eval_error"):
        return "hard_failure", "score_eval_error", False
    if failure == "no_telemetry":
        return "hard_failure", "no_telemetry", False
    return "hard_failure", "hard_failure", False


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
        transport_factory: TransportFactory | None = None,
    ) -> None:
        self.plan = plan
        self.plan_path = plan_path
        self.log_dir = log_dir
        self.event_handler = event_handler
        self.transport_factory = transport_factory or PySerialTransport.from_config
        self.stop_requested = False
        self.pause_requested = False
        self._paused_return_state: str | None = None
        self.session_id = f"{self.plan.get('plan_name', 'mcu_tuning')}_{now_stamp()}"
        self.state = self.STATE_IDLE
        self.current_parameter_key: str | None = None
        self.current_round_index: int | None = None
        self._active_serial: SessionTransport | None = None
        self.baseline_parameters: dict[str, float] | None = None
        self.active_parameter_keys: list[str] | None = None
        self.runtime_limit_overrides: dict[str, int | float] = {}
        self.touched_parameter_keys: set[str] = set()
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
        self._pending_log_records: list[dict[str, Any]] = []

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
        if event_type == "error":
            event.data.setdefault("message", event.message)
        if self._should_log_event(event):
            if self.log_path is not None:
                log_record(self.log_path, event.to_record())
            else:
                self._pending_log_records.append(event.to_record())
        if self.event_handler is not None:
            self.event_handler(event)

    def _should_log_event(self, event: TuningEvent) -> bool:
        return event.type in {"state", "error"} or (
            event.type == "action" and event.data.get("action") == "set_runtime_limits"
        )

    def _emit_error_event(
        self,
        category: str,
        code: str,
        message: str,
        *,
        recoverable: bool,
        action: str,
        **context: Any,
    ) -> None:
        data: dict[str, Any] = {
            "category": category,
            "code": code,
            "error_code": code,
            "recoverable": recoverable,
            "state": self.state,
            "action": action,
            "session_id": self.session_id,
        }
        if context:
            data["context"] = context
        self.emit("error", message, **data)

    def _execution_error(
        self,
        message: str,
        *,
        category: str,
        code: str,
        recoverable: bool,
        action: str,
        **context: Any,
    ) -> ExecutionError:
        return ExecutionError(
            message,
            category=category,
            code=code,
            recoverable=recoverable,
            action=action,
            context=context,
        )

    def _emit_command_error(
        self,
        result: CommandResult,
        *,
        action: str,
        message: str | None = None,
        **context: Any,
    ) -> None:
        category = result.category or "hard_failure"
        code = result.code or category
        error_message = message or result.error or f"{action} failed."
        self._emit_error_event(
            category,
            code,
            error_message,
            recoverable=result.recoverable,
            action=action,
            lines=list(result.lines),
            **context,
        )

    def _emit_trial_failure_error(
        self,
        trial: TrialResult,
        failure: str,
        *,
        action: str,
        **context: Any,
    ) -> None:
        category, code, recoverable = standard_error_fields(failure)
        message = trial.failure_messages.get(failure) or f"{action} failed: {failure}"
        self._emit_error_event(
            category,
            code,
            message,
            recoverable=recoverable,
            action=action,
            hard_failures=list(trial.hard_failures),
            **context,
        )

    def _emit_failures_as_error(
        self,
        failures: list[str],
        *,
        action: str,
        message_prefix: str,
        **context: Any,
    ) -> None:
        if not failures:
            return
        category, code, recoverable = standard_error_fields(failures[0])
        self._emit_error_event(
            category,
            code,
            f"{message_prefix}: {', '.join(failures)}",
            recoverable=recoverable,
            action=action,
            hard_failures=list(failures),
            **context,
        )

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

    def _parameter_keys_for_summary(self) -> list[str]:
        keys: list[str] = []
        for key in self.plan["step_policy"].get("parameter_order", []):
            if key in self.parameters and key not in keys:
                keys.append(key)
        for key in self.parameters:
            if key not in keys:
                keys.append(key)
        return keys

    def _tuning_parameter_order(self) -> list[str]:
        default_order = [
            key
            for key in self.plan["step_policy"].get("parameter_order", [])
            if key in self.parameters
        ]
        if self.active_parameter_keys is None:
            return default_order

        selected = set(self.active_parameter_keys)
        order = [key for key in default_order if key in selected]
        for key in self.active_parameter_keys:
            if key not in order:
                order.append(key)
        return order

    def _active_parameter_keys_for_summary(self) -> list[str]:
        return self._tuning_parameter_order()

    def _untouched_parameter_keys_for_summary(self) -> list[str]:
        skipped = set(self.skipped_parameter_keys)
        touched = set(self.touched_parameter_keys)
        return [
            key
            for key in self._parameter_keys_for_summary()
            if key not in touched and key not in skipped
        ]

    def set_active_parameters(self, keys: list[str]) -> ActionResult:
        if self.state != self.STATE_IDLE or self.log_path is not None:
            return self._reject_action(
                "set_active_parameters",
                "session_already_started",
                "Active parameters can only be selected before the run starts.",
            )
        if isinstance(keys, str) or not isinstance(keys, (list, tuple)):
            return self._reject_action(
                "set_active_parameters",
                "invalid_active_parameter_selection",
                "Active parameter selection must be a list of parameter keys.",
            )

        normalized: list[str] = []
        invalid_keys: list[Any] = []
        for key in keys:
            if not isinstance(key, str) or not key:
                invalid_keys.append(key)
                continue
            if key not in normalized:
                normalized.append(key)
        if invalid_keys:
            return self._reject_action(
                "set_active_parameters",
                "invalid_parameter_key",
                "Active parameter keys must be non-empty strings.",
                invalid_parameter_keys=invalid_keys,
            )
        if not normalized:
            return self._reject_action(
                "set_active_parameters",
                "empty_active_parameter_selection",
                "At least one active parameter must be selected.",
            )

        unknown_keys = [key for key in normalized if key not in self.parameters]
        if unknown_keys:
            return self._reject_action(
                "set_active_parameters",
                "unknown_parameter_key",
                "Active parameter selection includes keys that are not declared in YAML.",
                unknown_parameter_keys=unknown_keys,
                declared_parameter_keys=self._parameter_keys_for_summary(),
            )

        previous_keys = self._active_parameter_keys_for_summary()
        self.active_parameter_keys = list(normalized)
        active_keys = self._active_parameter_keys_for_summary()
        untouched_keys = self._untouched_parameter_keys_for_summary()
        result = ActionResult(
            ok=True,
            action="set_active_parameters",
            state=self.state,
            message="Active parameters selected.",
            data={
                "active_parameter_keys": active_keys,
                "previous_active_parameter_keys": previous_keys,
                "untouched_parameter_keys": untouched_keys,
            },
        )
        self.emit(
            "action",
            result.message,
            action="set_active_parameters",
            state=self.state,
            session_id=self.session_id,
            active_parameter_keys=active_keys,
            previous_active_parameter_keys=previous_keys,
            untouched_parameter_keys=untouched_keys,
        )
        return result

    def _yaml_runtime_limits(self) -> dict[str, int | float]:
        return {
            "max_rounds": int(self.plan["step_policy"]["max_rounds"]),
            "trial_window_ms": float(self.plan["scoring"]["trial_window_ms"]),
            "cooldown_ms": float(self.plan["step_policy"]["cooldown_ms"]),
        }

    def _effective_runtime_limits(self) -> dict[str, int | float]:
        limits = self._yaml_runtime_limits()
        limits.update(self.runtime_limit_overrides)
        return limits

    def set_runtime_limits(
        self,
        *,
        max_rounds: int | None = None,
        trial_window_ms: Number | None = None,
        cooldown_ms: Number | None = None,
    ) -> ActionResult:
        if self.state != self.STATE_IDLE or self.log_path is not None:
            return self._reject_action(
                "set_runtime_limits",
                "session_already_started",
                "Runtime limits can only be overridden before the run starts.",
            )

        requested = {
            "max_rounds": max_rounds,
            "trial_window_ms": trial_window_ms,
            "cooldown_ms": cooldown_ms,
        }
        requested = {key: value for key, value in requested.items() if value is not None}
        if not requested:
            return self._reject_action(
                "set_runtime_limits",
                "empty_runtime_limit_override",
                "At least one runtime limit override is required.",
            )

        invalid_limits: dict[str, Any] = {}
        normalized: dict[str, int | float] = {}
        for key, value in requested.items():
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                invalid_limits[key] = value
                continue
            if not math.isfinite(float(value)):
                invalid_limits[key] = value
                continue
            if key == "max_rounds":
                if int(value) != value or value < 0:
                    invalid_limits[key] = value
                    continue
                normalized[key] = int(value)
            else:
                if value <= 0:
                    invalid_limits[key] = value
                    continue
                normalized[key] = float(value)
        if invalid_limits:
            return self._reject_action(
                "set_runtime_limits",
                "invalid_runtime_limit",
                "Runtime limit overrides must be numeric and within allowed ranges.",
                invalid_runtime_limits=invalid_limits,
            )

        yaml_limits = self._yaml_runtime_limits()
        exceeded_limits = {
            key: {
                "requested": value,
                "yaml_limit": yaml_limits[key],
            }
            for key, value in normalized.items()
            if value > yaml_limits[key]
        }
        if exceeded_limits:
            return self._reject_action(
                "set_runtime_limits",
                "runtime_limit_exceeds_yaml",
                "Runtime limit overrides cannot exceed YAML limits.",
                exceeded_runtime_limits=exceeded_limits,
                requested_runtime_limits=normalized,
                yaml_runtime_limits=yaml_limits,
            )

        previous_overrides = dict(self.runtime_limit_overrides)
        self.runtime_limit_overrides.update(normalized)
        effective_limits = self._effective_runtime_limits()
        result = ActionResult(
            ok=True,
            action="set_runtime_limits",
            state=self.state,
            message="Runtime limits overridden.",
            data={
                "runtime_limit_overrides": dict(self.runtime_limit_overrides),
                "previous_runtime_limit_overrides": previous_overrides,
                "requested_runtime_limits": dict(normalized),
                "yaml_runtime_limits": yaml_limits,
                "effective_runtime_limits": effective_limits,
            },
        )
        self.emit(
            "action",
            result.message,
            action="set_runtime_limits",
            state=self.state,
            session_id=self.session_id,
            **result.data,
        )
        return result

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
                self._emit_error_event(
                    "rollback_failed",
                    "rollback_failed",
                    message,
                    recoverable=False,
                    action="rollback_to",
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
        for record in self._pending_log_records:
            log_record(self.log_path, record)
        self._pending_log_records.clear()
        return self.log_path

    def execute_rollback(self, ser: SessionTransport, key: str, value: Number) -> CommandResult:
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
                return CommandResult(
                    False,
                    status.lines,
                    f"rollback readback mismatch for {key}",
                    "readback_mismatch",
                    "readback_mismatch",
                    True,
                )
        return result

    def stop_device(self, ser: SessionTransport) -> None:
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
        ser: SessionTransport,
        param: dict[str, Any],
        trial_value: float,
        baseline_score: float,
    ) -> TrialResult:
        commands = self.plan["commands"]
        transport = self.plan["transport"]
        scoring = self.plan["scoring"]
        runtime_limits = self._effective_runtime_limits()

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
            failure = set_result.code or "ok_timeout"
            return TrialResult(
                "rollback",
                None,
                None,
                [failure],
                TelemetryResult([], 0, set_result.lines),
                {failure: set_result.error or "SET command failed."},
            )

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
                failure = status.code or "readback_status_failed"
                return TrialResult(
                    "rollback",
                    None,
                    None,
                    [failure],
                    TelemetryResult([], 0, status.lines),
                    {failure: status.error or "Readback STATUS failed."},
                )
            readback_key = param.get("readback_key") or param["key"]
            if not status_confirms_value(status.lines, readback_key, trial_value):
                message = f"readback mismatch for {readback_key}: expected {trial_value:g}"
                return TrialResult(
                    "rollback",
                    None,
                    None,
                    ["readback_mismatch"],
                    TelemetryResult([], 0, status.lines),
                    {"readback_mismatch": message},
                )

        time.sleep(float(runtime_limits["cooldown_ms"]) / 1000.0)
        telemetry = collect_telemetry(ser, self.plan, int(runtime_limits["trial_window_ms"]), self.event_handler)
        score, hard_failures = score_window(self.plan, telemetry)
        if score is None:
            return TrialResult(
                "rollback",
                None,
                None,
                hard_failures,
                telemetry,
                {failure: f"trial failed: {failure}" for failure in hard_failures},
            )

        improvement = improvement_ratio(baseline_score, score, bool(scoring["lower_is_better"]))
        if hard_failures:
            decision = "rollback"
        elif improvement >= float(scoring["accept_if_improvement_gte"]):
            decision = "accept"
        elif improvement <= -float(scoring["rollback_if_degradation_gte"]):
            decision = "rollback"
        else:
            decision = "hold"
        return TrialResult(
            decision,
            score,
            improvement,
            hard_failures,
            telemetry,
            {failure: f"trial failed: {failure}" for failure in hard_failures},
        )

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
        runtime_limits = self._effective_runtime_limits()

        self.emit("info", f"Opening serial port {transport['port']} @ {transport['baudrate']}...")

        try:
            serial_handle = self.transport_factory(transport).open()
        except serial.SerialException as exc:
            message = f"serial port failure: {exc}"
            self._set_state(self.STATE_ERROR, "serial_exception")
            self._emit_error_event(
                "serial_exception",
                "serial_exception",
                message,
                recoverable=False,
                action="open_serial",
                port=transport.get("port"),
            )
            raise self._execution_error(
                message,
                category="serial_exception",
                code="serial_exception",
                recoverable=False,
                action="open_serial",
                port=transport.get("port"),
            ) from exc

        with serial_handle as ser:
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
                    self._emit_command_error(status, action="status", command=commands["status"])
                    raise self._execution_error(
                        status.error or "initial STATUS failed",
                        category=status.category or "hard_failure",
                        code=status.code or "initial_status_failed",
                        recoverable=status.recoverable,
                        action="status",
                        command=commands["status"],
                    )

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
                    self._emit_command_error(start, action="telemetry_on", command=commands["telemetry_on"])
                    raise self._execution_error(
                        start.error or "telemetry_on failed",
                        category=start.category or "hard_failure",
                        code=start.code or "telemetry_on_failed",
                        recoverable=start.recoverable,
                        action="telemetry_on",
                        command=commands["telemetry_on"],
                    )

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
                    self._emit_failures_as_error(
                        baseline_failures,
                        action="baseline",
                        message_prefix="baseline failed",
                        telemetry_summary={
                            "samples": len(baseline_telemetry.samples),
                            "malformed_count": baseline_telemetry.malformed_count,
                        },
                    )
                    category, code, recoverable = standard_error_fields(
                        baseline_failures[0] if baseline_failures else "hard_failure"
                    )
                    raise self._execution_error(
                        f"baseline failed: {', '.join(baseline_failures)}",
                        category=category,
                        code=code,
                        recoverable=recoverable,
                        action="baseline",
                    )
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
                parameter_order = self._tuning_parameter_order()
                for round_index in range(1, int(runtime_limits["max_rounds"]) + 1):
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
                    self.touched_parameter_keys.add(key)
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
                            self._emit_error_event(
                                "rollback_failed",
                                "rollback_failed",
                                f"Rollback failed for {key}: {rollback.error}",
                                recoverable=False,
                                action="rollback",
                                key=key,
                                round=round_index,
                                rollback_status=rollback_status,
                                lines=list(rollback.lines),
                            )
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
                        self._emit_trial_failure_error(
                            trial,
                            trial.hard_failures[0],
                            action="run_trial",
                            key=key,
                            round=round_index,
                            decision=trial.decision,
                            rollback_status=rollback_status,
                            telemetry_summary=record["telemetry_summary"],
                        )
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
            "active_parameter_keys": self._active_parameter_keys_for_summary(),
            "runtime_limit_overrides": dict(self.runtime_limit_overrides),
            "yaml_runtime_limits": self._yaml_runtime_limits(),
            "effective_runtime_limits": self._effective_runtime_limits(),
            "touched_parameter_keys": [
                key for key in self._parameter_keys_for_summary() if key in self.touched_parameter_keys
            ],
            "skipped_parameters": self.skipped_parameters,
            "skipped_parameter_keys": sorted(self.skipped_parameter_keys),
            "untouched_parameter_keys": self._untouched_parameter_keys_for_summary(),
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
