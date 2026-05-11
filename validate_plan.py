#!/usr/bin/env python
"""Validate an executor-ready MCU Bluetooth tuning YAML plan.

This script is intentionally offline: it never opens a serial port and never
sends device commands. It only checks that a plan is structurally complete and
safe enough for a later executor step to consider running.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError as exc:  # pragma: no cover - environment dependent
    print("ERROR: PyYAML is required. Install with: python -m pip install PyYAML", file=sys.stderr)
    raise SystemExit(3) from exc


TOP_LEVEL_KEYS = {
    "schema_version",
    "plan_name",
    "source",
    "transport",
    "commands",
    "parameters",
    "telemetry",
    "scoring",
    "step_policy",
    "rollback",
    "stop_conditions",
    "recording",
}

TRANSPORT_REQUIRED = {
    "port",
    "baudrate",
    "data_bits",
    "parity",
    "stop_bits",
    "line_ending",
    "read_timeout_ms",
    "write_timeout_ms",
}

COMMANDS_REQUIRED = {
    "set",
    "status",
    "telemetry_on",
    "telemetry_off",
    "stop",
    "ok_pattern",
    "error_patterns",
    "readback_required",
}

PARAMETER_REQUIRED = {
    "key",
    "meaning",
    "current",
    "min",
    "max",
    "initial_step",
    "max_delta_per_round",
    "runtime_safe",
    "role",
    "group",
    "readback_key",
    "telemetry_effect_fields",
}

TELEMETRY_FIELD_REQUIRED = {"name", "unit", "type", "scale", "role"}
ALLOWED_ROLES = {"primary", "secondary", "observe-only", "unsafe"}
ALLOWED_TELEMETRY_FORMATS = {"csv", "json", "binary", "line_regex"}
ALLOWED_RESTORE_TARGETS = {"last_stable", "baseline"}
ALLOWED_PARITY = {"none", "even", "odd"}


class Validator:
    def __init__(self) -> None:
        self.errors: list[str] = []
        self.warnings: list[str] = []

    def error(self, path: str, message: str) -> None:
        self.errors.append(f"{path}: {message}")

    def warn(self, path: str, message: str) -> None:
        self.warnings.append(f"{path}: {message}")

    def require_mapping(self, value: Any, path: str) -> dict[str, Any] | None:
        if not isinstance(value, dict):
            self.error(path, f"expected mapping, got {type(value).__name__}")
            return None
        return value

    def require_list(self, value: Any, path: str, non_empty: bool = True) -> list[Any] | None:
        if not isinstance(value, list):
            self.error(path, f"expected list, got {type(value).__name__}")
            return None
        if non_empty and not value:
            self.error(path, "must not be empty")
        return value

    def require_keys(self, mapping: dict[str, Any], keys: set[str], path: str) -> None:
        missing = sorted(keys - set(mapping))
        for key in missing:
            self.error(f"{path}.{key}", "missing required field")

    def require_string(self, value: Any, path: str, allow_empty: bool = False) -> str | None:
        if not isinstance(value, str):
            self.error(path, f"expected string, got {type(value).__name__}")
            return None
        if not allow_empty and not value:
            self.error(path, "must not be empty")
        return value

    def require_number(self, value: Any, path: str, positive: bool = False) -> float | None:
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            self.error(path, f"expected number, got {type(value).__name__}")
            return None
        number = float(value)
        if positive and number <= 0:
            self.error(path, "must be > 0")
        return number

    def require_bool(self, value: Any, path: str) -> bool | None:
        if not isinstance(value, bool):
            self.error(path, f"expected boolean, got {type(value).__name__}")
            return None
        return value


def load_plan(path: Path) -> Any:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return yaml.safe_load(handle)
    except FileNotFoundError:
        print(f"ERROR: file not found: {path}", file=sys.stderr)
        raise SystemExit(2)
    except yaml.YAMLError as exc:
        print(f"ERROR: YAML parse failed: {exc}", file=sys.stderr)
        raise SystemExit(2)


def extract_metric_fields(metric: str) -> set[str]:
    fields: set[str] = set()
    for match in re.finditer(r"\(([^()]+)\)", metric):
        inner = match.group(1).strip()
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", inner):
            fields.add(inner)
    return fields


def validate_source(v: Validator, source: dict[str, Any]) -> None:
    v.require_keys(source, {"project_path", "generated_from", "confirmed_by_user"}, "source")
    if "project_path" in source and source["project_path"] is not None:
        v.require_string(source["project_path"], "source.project_path")
    generated_from = v.require_list(source.get("generated_from"), "source.generated_from", non_empty=False)
    if generated_from is not None:
        for index, item in enumerate(generated_from):
            v.require_string(item, f"source.generated_from[{index}]")
    confirmed_by_user = v.require_list(
        source.get("confirmed_by_user"), "source.confirmed_by_user", non_empty=False
    )
    if confirmed_by_user is not None:
        for index, item in enumerate(confirmed_by_user):
            v.require_string(item, f"source.confirmed_by_user[{index}]")


def validate_transport(v: Validator, transport: dict[str, Any]) -> None:
    v.require_keys(transport, TRANSPORT_REQUIRED, "transport")
    v.require_string(transport.get("port"), "transport.port")
    baudrate = v.require_number(transport.get("baudrate"), "transport.baudrate", positive=True)
    if baudrate is not None and int(baudrate) != baudrate:
        v.error("transport.baudrate", "must be an integer")
    data_bits = v.require_number(transport.get("data_bits"), "transport.data_bits", positive=True)
    if data_bits is not None and int(data_bits) not in {5, 6, 7, 8}:
        v.error("transport.data_bits", "must be one of 5, 6, 7, 8")
    parity = v.require_string(transport.get("parity"), "transport.parity")
    if parity is not None and parity.lower() not in ALLOWED_PARITY:
        v.error("transport.parity", f"must be one of {sorted(ALLOWED_PARITY)}")
    stop_bits = v.require_number(transport.get("stop_bits"), "transport.stop_bits", positive=True)
    if stop_bits is not None and stop_bits not in {1, 1.5, 2}:
        v.error("transport.stop_bits", "must be one of 1, 1.5, 2")
    line_ending = v.require_string(transport.get("line_ending"), "transport.line_ending")
    if line_ending is not None and "\n" not in line_ending and "\r" not in line_ending:
        v.warn("transport.line_ending", "does not contain CR or LF")
    v.require_number(transport.get("read_timeout_ms"), "transport.read_timeout_ms", positive=True)
    v.require_number(transport.get("write_timeout_ms"), "transport.write_timeout_ms", positive=True)


def validate_commands(v: Validator, commands: dict[str, Any]) -> None:
    v.require_keys(commands, COMMANDS_REQUIRED, "commands")
    set_template = v.require_string(commands.get("set"), "commands.set")
    if set_template is not None:
        if "{key}" not in set_template:
            v.error("commands.set", "must contain {key}")
        if "{value}" not in set_template:
            v.error("commands.set", "must contain {value}")
    for key in ["status", "telemetry_on", "telemetry_off", "stop", "ok_pattern"]:
        v.require_string(commands.get(key), f"commands.{key}")
    if "start" in commands and commands["start"] is not None:
        v.require_string(commands["start"], "commands.start")
    error_patterns = v.require_list(commands.get("error_patterns"), "commands.error_patterns", non_empty=False)
    if error_patterns is not None:
        for index, item in enumerate(error_patterns):
            v.require_string(item, f"commands.error_patterns[{index}]")
    v.require_bool(commands.get("readback_required"), "commands.readback_required")


def validate_parameters(
    v: Validator, parameters: list[Any], commands: dict[str, Any]
) -> tuple[dict[str, dict[str, Any]], set[str]]:
    param_by_key: dict[str, dict[str, Any]] = {}
    effect_fields: set[str] = set()
    readback_required = commands.get("readback_required") is True

    for index, raw_param in enumerate(parameters):
        path = f"parameters[{index}]"
        param = v.require_mapping(raw_param, path)
        if param is None:
            continue
        v.require_keys(param, PARAMETER_REQUIRED, path)
        key = v.require_string(param.get("key"), f"{path}.key")
        if key:
            if key in param_by_key:
                v.error(f"{path}.key", f"duplicate parameter key: {key}")
            else:
                param_by_key[key] = param
        v.require_string(param.get("meaning"), f"{path}.meaning")
        minimum = v.require_number(param.get("min"), f"{path}.min")
        maximum = v.require_number(param.get("max"), f"{path}.max")
        current = v.require_number(param.get("current"), f"{path}.current")
        initial_step = v.require_number(param.get("initial_step"), f"{path}.initial_step", positive=True)
        max_delta = v.require_number(param.get("max_delta_per_round"), f"{path}.max_delta_per_round", positive=True)
        if minimum is not None and maximum is not None and minimum >= maximum:
            v.error(path, "min must be < max")
        if current is not None and minimum is not None and current < minimum:
            v.error(f"{path}.current", "must be >= min")
        if current is not None and maximum is not None and current > maximum:
            v.error(f"{path}.current", "must be <= max")
        if initial_step is not None and max_delta is not None and initial_step > max_delta:
            v.error(f"{path}.initial_step", "must be <= max_delta_per_round")
        runtime_safe = v.require_bool(param.get("runtime_safe"), f"{path}.runtime_safe")
        role = v.require_string(param.get("role"), f"{path}.role")
        if role is not None and role not in ALLOWED_ROLES:
            v.error(f"{path}.role", f"must be one of {sorted(ALLOWED_ROLES)}")
        if runtime_safe is True and role == "unsafe":
            v.error(path, "unsafe parameter cannot be runtime_safe")
        if runtime_safe is False and role in {"primary", "secondary"}:
            v.error(path, "primary/secondary parameter must be runtime_safe")
        v.require_string(param.get("group"), f"{path}.group")
        readback_key = v.require_string(param.get("readback_key"), f"{path}.readback_key", allow_empty=True)
        if readback_required and readback_key == "":
            v.error(f"{path}.readback_key", "must not be empty when readback is required")
        telemetry_effect_fields = v.require_list(
            param.get("telemetry_effect_fields"), f"{path}.telemetry_effect_fields", non_empty=False
        )
        if telemetry_effect_fields is not None:
            for field_index, field in enumerate(telemetry_effect_fields):
                value = v.require_string(field, f"{path}.telemetry_effect_fields[{field_index}]")
                if value:
                    effect_fields.add(value)

    return param_by_key, effect_fields


def validate_telemetry(v: Validator, telemetry: dict[str, Any]) -> set[str]:
    v.require_keys(
        telemetry,
        {"format", "prefix", "delimiter", "sample_period_ms", "fields", "malformed_policy"},
        "telemetry",
    )
    fmt = v.require_string(telemetry.get("format"), "telemetry.format")
    if fmt is not None and fmt not in ALLOWED_TELEMETRY_FORMATS:
        v.error("telemetry.format", f"must be one of {sorted(ALLOWED_TELEMETRY_FORMATS)}")
    if telemetry.get("prefix") is not None:
        v.require_string(telemetry["prefix"], "telemetry.prefix")
    if fmt == "csv":
        v.require_string(telemetry.get("delimiter"), "telemetry.delimiter")
    elif telemetry.get("delimiter") is not None:
        v.require_string(telemetry["delimiter"], "telemetry.delimiter")
    v.require_number(telemetry.get("sample_period_ms"), "telemetry.sample_period_ms", positive=True)
    v.require_string(telemetry.get("malformed_policy"), "telemetry.malformed_policy")

    fields = v.require_list(telemetry.get("fields"), "telemetry.fields")
    names: set[str] = set()
    if fields is None:
        return names

    for index, raw_field in enumerate(fields):
        path = f"telemetry.fields[{index}]"
        field = v.require_mapping(raw_field, path)
        if field is None:
            continue
        v.require_keys(field, TELEMETRY_FIELD_REQUIRED, path)
        name = v.require_string(field.get("name"), f"{path}.name")
        if name:
            if name in names:
                v.error(f"{path}.name", f"duplicate telemetry field: {name}")
            names.add(name)
        v.require_string(field.get("unit"), f"{path}.unit", allow_empty=True)
        v.require_string(field.get("type"), f"{path}.type")
        v.require_number(field.get("scale"), f"{path}.scale", positive=True)
        v.require_string(field.get("role"), f"{path}.role")

    return names


def validate_scoring(v: Validator, scoring: dict[str, Any], telemetry_fields: set[str]) -> set[str]:
    v.require_keys(
        scoring,
        {
            "lower_is_better",
            "baseline_window_ms",
            "trial_window_ms",
            "metrics",
            "formula",
            "accept_if_improvement_gte",
            "rollback_if_degradation_gte",
            "hard_failures",
        },
        "scoring",
    )
    v.require_bool(scoring.get("lower_is_better"), "scoring.lower_is_better")
    v.require_number(scoring.get("baseline_window_ms"), "scoring.baseline_window_ms", positive=True)
    v.require_number(scoring.get("trial_window_ms"), "scoring.trial_window_ms", positive=True)
    metrics = v.require_list(scoring.get("metrics"), "scoring.metrics")
    metric_fields: set[str] = set()
    if metrics is not None:
        for index, metric in enumerate(metrics):
            metric_string = v.require_string(metric, f"scoring.metrics[{index}]")
            if metric_string:
                metric_fields.update(extract_metric_fields(metric_string))
    v.require_string(scoring.get("formula"), "scoring.formula")
    accept = v.require_number(scoring.get("accept_if_improvement_gte"), "scoring.accept_if_improvement_gte")
    rollback = v.require_number(scoring.get("rollback_if_degradation_gte"), "scoring.rollback_if_degradation_gte")
    if accept is not None and accept <= 0:
        v.error("scoring.accept_if_improvement_gte", "must be > 0")
    if rollback is not None and rollback <= 0:
        v.error("scoring.rollback_if_degradation_gte", "must be > 0")
    hard_failures = v.require_list(scoring.get("hard_failures"), "scoring.hard_failures")
    if hard_failures is not None:
        for index, item in enumerate(hard_failures):
            v.require_string(item, f"scoring.hard_failures[{index}]")

    missing = sorted(metric_fields - telemetry_fields)
    for field in missing:
        v.error("scoring.metrics", f"references unknown telemetry field: {field}")
    return metric_fields


def validate_step_policy(
    v: Validator, step_policy: dict[str, Any], param_by_key: dict[str, dict[str, Any]]
) -> None:
    v.require_keys(
        step_policy,
        {
            "mode",
            "parameter_order",
            "shrink_on_failure",
            "expand_on_success",
            "max_rounds",
            "cooldown_ms",
        },
        "step_policy",
    )
    v.require_string(step_policy.get("mode"), "step_policy.mode")
    order = v.require_list(step_policy.get("parameter_order"), "step_policy.parameter_order")
    if order is not None:
        for index, key in enumerate(order):
            value = v.require_string(key, f"step_policy.parameter_order[{index}]")
            if not value:
                continue
            param = param_by_key.get(value)
            if param is None:
                v.error(f"step_policy.parameter_order[{index}]", f"unknown parameter: {value}")
                continue
            if param.get("runtime_safe") is not True:
                v.error(f"step_policy.parameter_order[{index}]", f"parameter is not runtime_safe: {value}")
            if param.get("role") in {"unsafe", "observe-only"}:
                v.error(f"step_policy.parameter_order[{index}]", f"parameter role is not tunable: {value}")
    shrink = v.require_number(step_policy.get("shrink_on_failure"), "step_policy.shrink_on_failure", positive=True)
    if shrink is not None and shrink >= 1:
        v.error("step_policy.shrink_on_failure", "must be < 1")
    v.require_bool(step_policy.get("expand_on_success"), "step_policy.expand_on_success")
    max_rounds = v.require_number(step_policy.get("max_rounds"), "step_policy.max_rounds", positive=True)
    if max_rounds is not None and int(max_rounds) != max_rounds:
        v.error("step_policy.max_rounds", "must be an integer")
    v.require_number(step_policy.get("cooldown_ms"), "step_policy.cooldown_ms", positive=True)


def validate_rollback(v: Validator, rollback: dict[str, Any]) -> None:
    v.require_keys(
        rollback,
        {"baseline_source", "restore_target", "command_template", "confirm_with", "on_failure"},
        "rollback",
    )
    v.require_string(rollback.get("baseline_source"), "rollback.baseline_source")
    restore_target = v.require_string(rollback.get("restore_target"), "rollback.restore_target")
    if restore_target is not None and restore_target not in ALLOWED_RESTORE_TARGETS:
        v.error("rollback.restore_target", f"must be one of {sorted(ALLOWED_RESTORE_TARGETS)}")
    command_template = v.require_string(rollback.get("command_template"), "rollback.command_template")
    if command_template is not None:
        if "{key}" not in command_template:
            v.error("rollback.command_template", "must contain {key}")
        if "{value}" not in command_template:
            v.error("rollback.command_template", "must contain {value}")
    v.require_string(rollback.get("confirm_with"), "rollback.confirm_with")
    on_failure = v.require_list(rollback.get("on_failure"), "rollback.on_failure")
    if on_failure is not None:
        for index, item in enumerate(on_failure):
            v.require_string(item, f"rollback.on_failure[{index}]")


def validate_stop_conditions(v: Validator, stop_conditions: dict[str, Any]) -> None:
    required = {"success", "repeated_degradation", "communication_failure", "unsafe_behavior", "manual_stop"}
    v.require_keys(stop_conditions, required, "stop_conditions")
    for key in sorted(required):
        v.require_string(stop_conditions.get(key), f"stop_conditions.{key}")


def validate_recording(v: Validator, recording: dict[str, Any]) -> None:
    v.require_keys(recording, {"per_round_fields", "save_recommended"}, "recording")
    fields = v.require_list(recording.get("per_round_fields"), "recording.per_round_fields")
    if fields is not None:
        for index, item in enumerate(fields):
            v.require_string(item, f"recording.per_round_fields[{index}]")
    v.require_bool(recording.get("save_recommended"), "recording.save_recommended")


def validate_plan(plan: Any) -> Validator:
    v = Validator()
    root = v.require_mapping(plan, "$")
    if root is None:
        return v

    v.require_keys(root, TOP_LEVEL_KEYS, "$")
    extra = sorted(set(root) - TOP_LEVEL_KEYS)
    for key in extra:
        v.warn(f"$.{key}", "unknown top-level key will be ignored by the first executor")

    schema_version = root.get("schema_version")
    if schema_version != 1:
        v.error("schema_version", "must be 1")
    v.require_string(root.get("plan_name"), "plan_name")

    source = v.require_mapping(root.get("source"), "source")
    if source is not None:
        validate_source(v, source)

    transport = v.require_mapping(root.get("transport"), "transport")
    if transport is not None:
        validate_transport(v, transport)

    commands = v.require_mapping(root.get("commands"), "commands")
    if commands is not None:
        validate_commands(v, commands)
    else:
        commands = {}

    parameters = v.require_list(root.get("parameters"), "parameters")
    param_by_key: dict[str, dict[str, Any]] = {}
    effect_fields: set[str] = set()
    if parameters is not None:
        param_by_key, effect_fields = validate_parameters(v, parameters, commands)

    telemetry = v.require_mapping(root.get("telemetry"), "telemetry")
    telemetry_fields: set[str] = set()
    if telemetry is not None:
        telemetry_fields = validate_telemetry(v, telemetry)

    for field in sorted(effect_fields - telemetry_fields):
        v.error("parameters.telemetry_effect_fields", f"references unknown telemetry field: {field}")

    scoring = v.require_mapping(root.get("scoring"), "scoring")
    if scoring is not None:
        validate_scoring(v, scoring, telemetry_fields)

    step_policy = v.require_mapping(root.get("step_policy"), "step_policy")
    if step_policy is not None:
        validate_step_policy(v, step_policy, param_by_key)

    rollback = v.require_mapping(root.get("rollback"), "rollback")
    if rollback is not None:
        validate_rollback(v, rollback)

    stop_conditions = v.require_mapping(root.get("stop_conditions"), "stop_conditions")
    if stop_conditions is not None:
        validate_stop_conditions(v, stop_conditions)

    recording = v.require_mapping(root.get("recording"), "recording")
    if recording is not None:
        validate_recording(v, recording)

    return v


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate an MCU Bluetooth tuning plan YAML file.")
    parser.add_argument("plan", type=Path, help="Path to mcu_tuning_plan.yaml")
    args = parser.parse_args(argv)

    plan = load_plan(args.plan)
    validator = validate_plan(plan)

    if validator.warnings:
        print("WARNINGS:")
        for warning in validator.warnings:
            print(f"  - {warning}")

    if validator.errors:
        print("ERRORS:")
        for error in validator.errors:
            print(f"  - {error}")
        print("RESULT: invalid")
        return 1

    print("RESULT: valid")
    print("Plan passed offline executor preflight validation.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
