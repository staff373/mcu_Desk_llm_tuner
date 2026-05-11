#!/usr/bin/env python
"""Agent-facing control facade and no-hardware probe for TuningSession."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from tuning_session import (
    ActionResult,
    EventHandler,
    ExecutionError,
    TransportFactory,
    TuningEvent,
    TuningSession,
    VirtualTransport,
    serial,
)
from validate_plan import load_plan as _load_plan
from validate_plan import validate_plan as _validate_plan


TransportFactoryProvider = Callable[[dict[str, Any]], TransportFactory]
SESSION_REPORT_SCHEMA_VERSION = 1
SESSION_REPORT_REQUIRED_FIELDS = (
    "schema_version",
    "report_type",
    "generated_at",
    "plan_path",
    "session_id",
    "state",
    "validated",
    "last_exit_code",
    "action_log",
    "event_counts",
    "final_summary",
    "artifact_paths",
    "artifact_exists",
    "serial_safety",
    "real_serial_status",
)

DEFAULT_VIRTUAL_PLAN_PATH = (
    Path(__file__).resolve().parent / "fixtures" / "virtual_mcu_tuning_plan.yaml"
)
REQUIRED_PROBE_ACTIONS = (
    "load_plan",
    "validate_plan",
    "rollback_to",
    "pause",
    "skip_current_param",
    "resume",
    "start_auto_tune",
    "stop",
    "export_session_report",
)
REQUIRED_PROBE_EVENT_TYPES = (
    "action",
    "baseline",
    "dat",
    "decision",
    "round",
    "rx",
    "state",
    "summary",
    "tx",
)


def validate_session_report_schema(report: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    for field in SESSION_REPORT_REQUIRED_FIELDS:
        if field not in report:
            problems.append(f"missing field: {field}")

    if report.get("schema_version") != SESSION_REPORT_SCHEMA_VERSION:
        problems.append("unsupported schema_version")
    if report.get("report_type") != "agent_session_report":
        problems.append("unsupported report_type")

    expected_types = {
        "action_log": list,
        "artifact_exists": dict,
        "artifact_paths": dict,
        "event_counts": dict,
    }
    for field, expected_type in expected_types.items():
        if field in report and not isinstance(report[field], expected_type):
            problems.append(f"field {field} must be {expected_type.__name__}")

    serial_safety = report.get("serial_safety")
    if not isinstance(serial_safety, dict):
        problems.append("serial_safety must be dict")
    else:
        for field in ("real_serial_status", "statement", "serial_open_attempts"):
            if field not in serial_safety:
                problems.append(f"serial_safety missing field: {field}")
        if not isinstance(serial_safety.get("serial_open_attempts", []), list):
            problems.append("serial_safety.serial_open_attempts must be list")

    return problems


class ProbeFailure(RuntimeError):
    """Raised when the no-hardware probe cannot prove the required paths."""


class _SerialOpenBlocked:
    opened_ports: list[str] = []

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        port = kwargs.get("port")
        if port is None and args:
            port = args[0]
        type(self).opened_ports.append(str(port or ""))
        raise RuntimeError("agent console probe must not open a real serial port")


@contextmanager
def _block_real_serial_opens() -> Any:
    original_serial = serial.Serial
    _SerialOpenBlocked.opened_ports = []
    serial.Serial = _SerialOpenBlocked
    try:
        yield _SerialOpenBlocked.opened_ports
    finally:
        serial.Serial = original_serial


class AgentControlFacade:
    def __init__(
        self,
        *,
        log_dir: Path | None = None,
        event_handler: EventHandler | None = None,
        transport_factory_provider: TransportFactoryProvider | None = None,
        serial_open_attempts: list[str] | None = None,
    ) -> None:
        self._log_dir = log_dir
        self._event_handler = event_handler
        self._transport_factory_provider = transport_factory_provider
        self._serial_open_attempts = serial_open_attempts
        self._plan: dict[str, Any] | None = None
        self._plan_path: Path | None = None
        self._validated = False
        self._session: TuningSession | None = None
        self._events: list[TuningEvent] = []
        self._action_log: list[dict[str, Any]] = []
        self._last_exit_code: int | None = None
        self._last_report_path: Path | None = None

    @property
    def state(self) -> str:
        if self._session is not None:
            return self._session.state
        if self._validated:
            return "validated"
        if self._plan is not None:
            return "loaded"
        return "unloaded"

    def load_plan(self, plan_path: Path | str) -> ActionResult:
        path = Path(plan_path)
        try:
            loaded = _load_plan(path)
        except SystemExit as exc:
            return self._record_result(
                ActionResult(
                    ok=False,
                    action="load_plan",
                    state=self.state,
                    message=f"Plan load failed: {path}",
                    error_code="plan_load_failed",
                    data={"plan_path": str(path), "exit_code": exc.code},
                )
            )

        self._plan = loaded
        self._plan_path = path
        self._validated = False
        self._session = None
        self._events.clear()
        self._action_log.clear()
        self._last_exit_code = None
        self._last_report_path = None
        return self._record_result(
            ActionResult(
                ok=True,
                action="load_plan",
                state=self.state,
                message="Plan loaded.",
                data={
                    "plan_path": str(path),
                    "plan_name": loaded.get("plan_name") if isinstance(loaded, dict) else None,
                },
            )
        )

    def validate_plan(self) -> ActionResult:
        if self._plan is None or self._plan_path is None:
            return self._reject("validate_plan", "plan_not_loaded", "Load a plan before validation.")

        validator = _validate_plan(self._plan)
        data = {
            "plan_path": str(self._plan_path),
            "warnings": list(validator.warnings),
            "errors": list(validator.errors),
        }
        if validator.errors:
            self._validated = False
            self._session = None
            return self._record_result(
                ActionResult(
                    ok=False,
                    action="validate_plan",
                    state=self.state,
                    message="Plan validation failed.",
                    error_code="plan_invalid",
                    data=data,
                )
            )

        self._validated = True
        self._session = self._create_session()
        return self._record_result(
            ActionResult(
                ok=True,
                action="validate_plan",
                state=self.state,
                message="Plan validated.",
                data=data,
            )
        )

    def start_auto_tune(self) -> ActionResult:
        guard = self._require_validated("start_auto_tune")
        if guard is not None:
            return guard
        session = self._require_session()

        try:
            exit_code = session.run()
        except serial.SerialException as exc:
            self._last_exit_code = 2
            return self._record_result(
                ActionResult(
                    ok=False,
                    action="start_auto_tune",
                    state=session.state,
                    message=f"Serial failure: {exc}",
                    error_code="serial_exception",
                    data={"exit_code": self._last_exit_code},
                )
            )
        except ExecutionError as exc:
            self._last_exit_code = 2
            return self._record_result(
                ActionResult(
                    ok=False,
                    action="start_auto_tune",
                    state=session.state,
                    message=str(exc),
                    error_code=exc.code,
                    data={
                        "exit_code": self._last_exit_code,
                        "category": exc.category,
                        "recoverable": exc.recoverable,
                        "context": exc.context,
                    },
                )
            )

        self._last_exit_code = exit_code
        return self._record_result(
            ActionResult(
                ok=exit_code == 0,
                action="start_auto_tune",
                state=session.state,
                message="Auto tune completed." if exit_code == 0 else "Auto tune failed.",
                error_code=None if exit_code == 0 else "execution_failed",
                data={"exit_code": exit_code, "summary": session.summary()},
            )
        )

    def pause(self) -> ActionResult:
        guard = self._require_validated("pause")
        if guard is not None:
            return guard
        return self._record_result(self._require_session().pause())

    def resume(self) -> ActionResult:
        guard = self._require_validated("resume")
        if guard is not None:
            return guard
        return self._record_result(self._require_session().resume())

    def skip_current_param(self, reason: str = "agent_skip") -> ActionResult:
        guard = self._require_validated("skip_current_param")
        if guard is not None:
            return guard
        return self._record_result(self._require_session().skip_current_param(reason=reason))

    def rollback_to(self, target: str) -> ActionResult:
        guard = self._require_validated("rollback_to")
        if guard is not None:
            return guard
        return self._record_result(self._require_session().rollback_to(target))

    def stop(self) -> ActionResult:
        guard = self._require_validated("stop")
        if guard is not None:
            return guard
        return self._record_result(self._require_session().request_stop())

    def export_session_report(self, output_path: Path | str | None = None) -> ActionResult:
        guard = self._require_validated("export_session_report")
        if guard is not None:
            return guard

        path = Path(output_path) if output_path is not None else self._default_report_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        result = ActionResult(
            ok=True,
            action="export_session_report",
            state=self.state,
            message="Session report exported.",
            data={"report_path": str(path)},
        )
        report = self._build_report(
            report_path=path,
            extra_action_records=[result.to_record()],
        )
        schema_errors = validate_session_report_schema(report)
        if schema_errors:
            return self._record_result(
                ActionResult(
                    ok=False,
                    action="export_session_report",
                    state=self.state,
                    message="Session report schema validation failed.",
                    error_code="report_schema_invalid",
                    data={"errors": schema_errors, "report_path": str(path)},
                )
            )
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        self._last_report_path = path
        self._action_log.append(result.to_record())
        return result

    def _create_session(self) -> TuningSession:
        if self._plan is None or self._plan_path is None:
            raise RuntimeError("cannot create a session before loading a plan")
        transport_factory = (
            self._transport_factory_provider(self._plan)
            if self._transport_factory_provider is not None
            else None
        )
        return TuningSession(
            self._plan,
            self._plan_path,
            log_dir=self._log_dir,
            event_handler=self._forward_event,
            transport_factory=transport_factory,
        )

    def _require_session(self) -> TuningSession:
        if self._session is None:
            self._session = self._create_session()
        return self._session

    def _require_validated(self, action: str) -> ActionResult | None:
        if self._validated:
            return None
        return self._reject(action, "plan_not_validated", "Validate the plan before this action.")

    def _reject(self, action: str, code: str, message: str, **data: Any) -> ActionResult:
        return self._record_result(
            ActionResult(
                ok=False,
                action=action,
                state=self.state,
                message=message,
                error_code=code,
                data=data,
            )
        )

    def _record_result(self, result: ActionResult) -> ActionResult:
        self._action_log.append(result.to_record())
        return result

    def _forward_event(self, event: TuningEvent) -> None:
        self._events.append(event)
        if self._event_handler is not None:
            self._event_handler(event)

    def _default_report_path(self) -> Path:
        root = self._log_dir
        if root is None:
            if self._plan_path is not None:
                root = self._plan_path.parent / "agent_reports"
            else:
                root = Path("agent_reports")
        session_id = self._session.session_id if self._session is not None else "no_session"
        return root / f"{session_id}_agent_report.json"

    def _serial_safety(self) -> dict[str, Any]:
        attempts = list(self._serial_open_attempts or [])
        virtual_or_custom_transport = self._transport_factory_provider is not None
        if attempts:
            status = "open attempted"
            statement = "A real serial port open was attempted during this report window."
        elif self._serial_open_attempts is not None:
            status = "not opened"
            statement = "Virtual/no-hardware run did not open COM8 or any real serial port."
        elif virtual_or_custom_transport:
            status = "not asserted"
            statement = "Custom or virtual transport was used, but real serial open attempts were not instrumented."
        else:
            status = "not asserted"
            statement = "Real serial status is not asserted for pyserial-backed runs."

        return {
            "real_serial_status": status,
            "statement": statement,
            "serial_open_attempts": attempts,
            "transport_mode": "custom_or_virtual" if virtual_or_custom_transport else "pyserial_default",
        }

    def _artifact_exists(
        self,
        artifact_paths: dict[str, str],
        *,
        pending_report_path: Path | None = None,
    ) -> dict[str, bool]:
        exists: dict[str, bool] = {}
        for name, raw_path in artifact_paths.items():
            path = Path(raw_path)
            exists[name] = path.exists() or (
                pending_report_path is not None
                and name == "report"
                and path == pending_report_path
            )
        return exists

    def _build_report(
        self,
        *,
        report_path: Path | None = None,
        extra_action_records: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        session = self._session
        artifact_paths: dict[str, str] = {}
        if report_path is not None:
            artifact_paths["report"] = str(report_path)
        if session is not None and session.log_path is not None:
            artifact_paths["session_log"] = str(session.log_path)
        if self._last_report_path is not None:
            artifact_paths["last_report"] = str(self._last_report_path)

        event_records = [event.to_record() for event in self._events]
        event_counts = Counter(record["type"] for record in event_records)
        action_log = list(self._action_log)
        if extra_action_records:
            action_log.extend(extra_action_records)
        serial_safety = self._serial_safety()
        return {
            "schema_version": SESSION_REPORT_SCHEMA_VERSION,
            "report_type": "agent_session_report",
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "plan_path": str(self._plan_path) if self._plan_path is not None else None,
            "plan_name": self._plan.get("plan_name") if isinstance(self._plan, dict) else None,
            "validated": self._validated,
            "session_id": session.session_id if session is not None else None,
            "state": self.state,
            "last_exit_code": self._last_exit_code,
            "action_log": action_log,
            "event_counts": dict(sorted(event_counts.items())),
            "events": event_records,
            "final_summary": session.summary() if session is not None else None,
            "artifact_paths": artifact_paths,
            "artifact_exists": self._artifact_exists(
                artifact_paths,
                pending_report_path=report_path,
            ),
            "serial_safety": serial_safety,
            "real_serial_status": serial_safety["real_serial_status"],
        }


def _default_probe_log_dir() -> Path:
    return Path("agent_probe_artifacts") / datetime.now().strftime("%Y%m%d-%H%M%S")


def _probe_problems(summary: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    failed_actions = [
        item["action"]
        for item in summary["action_results"]
        if not item.get("ok", False)
    ]
    if failed_actions:
        problems.append(f"failed actions: {', '.join(failed_actions)}")
    if summary["missing_required_actions"]:
        problems.append(
            "missing required actions: " + ", ".join(summary["missing_required_actions"])
        )
    if summary["missing_required_event_types"]:
        problems.append(
            "missing required event types: "
            + ", ".join(summary["missing_required_event_types"])
        )
    if summary["serial_open_attempts"]:
        problems.append(
            "real serial open attempted: " + ", ".join(summary["serial_open_attempts"])
        )
    if not summary["virtual_transport_closed"]:
        problems.append("virtual transport did not close")
    missing_artifacts = [
        name
        for name, exists in summary["artifact_exists"].items()
        if not exists
    ]
    if missing_artifacts:
        problems.append("missing artifacts: " + ", ".join(missing_artifacts))
    return problems


def _validate_probe_summary(summary: dict[str, Any]) -> None:
    problems = _probe_problems(summary)
    if problems:
        raise ProbeFailure("; ".join(problems))


def _build_probe_summary(
    *,
    report: dict[str, Any],
    action_results: list[ActionResult],
    serial_open_attempts: list[str],
    artifact_dir: Path,
    report_path: Path,
    summary_path: Path,
    required_event_types: tuple[str, ...],
    control_flags: dict[str, bool],
    virtual_transport_closed: bool | None,
) -> dict[str, Any]:
    action_records = [result.to_record() for result in action_results]
    action_names = [record["action"] for record in action_records]
    event_counts = dict(report.get("event_counts") or {})
    artifact_paths = dict(report.get("artifact_paths") or {})
    artifact_paths["report"] = str(report_path)
    artifact_paths["probe_summary"] = str(summary_path)

    session_log_path = artifact_paths.get("session_log")
    artifact_exists = {
        "report": report_path.exists(),
        "session_log": bool(session_log_path) and Path(session_log_path).exists(),
    }

    summary = {
        "ok": False,
        "plan_path": report.get("plan_path"),
        "artifact_dir": str(artifact_dir),
        "report_path": str(report_path),
        "summary_path": str(summary_path),
        "required_actions": list(REQUIRED_PROBE_ACTIONS),
        "required_event_types": list(required_event_types),
        "missing_required_actions": [
            action for action in REQUIRED_PROBE_ACTIONS if action not in action_names
        ],
        "missing_required_event_types": [
            event_type
            for event_type in required_event_types
            if int(event_counts.get(event_type, 0)) <= 0
        ],
        "action_results": action_records,
        "event_counts": event_counts,
        "artifact_paths": artifact_paths,
        "artifact_exists": artifact_exists,
        "control_flags": dict(control_flags),
        "virtual_transport_closed": virtual_transport_closed,
        "serial_open_attempts": list(serial_open_attempts),
        "real_serial_status": "not opened" if not serial_open_attempts else "open attempted",
        "final_state": report.get("state"),
        "last_exit_code": report.get("last_exit_code"),
    }
    summary["problems"] = _probe_problems(summary)
    summary["ok"] = not summary["problems"]
    return summary


def run_probe(
    *,
    plan_path: Path | str = DEFAULT_VIRTUAL_PLAN_PATH,
    log_dir: Path | str | None = None,
    report_path: Path | str | None = None,
    summary_path: Path | str | None = None,
    extra_required_event_types: list[str] | None = None,
) -> dict[str, Any]:
    plan = Path(plan_path)
    artifact_dir = Path(log_dir) if log_dir is not None else _default_probe_log_dir()
    artifact_dir.mkdir(parents=True, exist_ok=True)
    report = Path(report_path) if report_path is not None else artifact_dir / "agent_probe_report.json"
    summary = (
        Path(summary_path)
        if summary_path is not None
        else artifact_dir / "agent_probe_summary.json"
    )
    report.parent.mkdir(parents=True, exist_ok=True)
    summary.parent.mkdir(parents=True, exist_ok=True)

    required_event_types = tuple(
        dict.fromkeys(
            [
                *REQUIRED_PROBE_EVENT_TYPES,
                *(extra_required_event_types or []),
            ]
        )
    )
    action_results: list[ActionResult] = []
    control_flags = {"rollback": False, "pause": False, "skip": False, "resume": False}
    transport_holder: dict[str, VirtualTransport] = {}
    facade: AgentControlFacade

    def provider(loaded_plan: dict[str, Any]) -> TransportFactory:
        def factory(_transport: dict[str, Any]) -> VirtualTransport:
            transport = VirtualTransport(loaded_plan)
            transport_holder["transport"] = transport
            return transport

        return factory

    def on_event(event: TuningEvent) -> None:
        if event.type == "baseline" and not control_flags["rollback"]:
            control_flags["rollback"] = True
            action_results.append(facade.rollback_to("baseline"))
        if event.type == "round" and not control_flags["pause"]:
            control_flags["pause"] = True
            action_results.append(facade.pause())
            control_flags["skip"] = True
            action_results.append(facade.skip_current_param("agent_probe_skip"))
        if (
            event.type == "state"
            and event.data.get("next_state") == "paused"
            and not control_flags["resume"]
        ):
            control_flags["resume"] = True
            action_results.append(facade.resume())

    with _block_real_serial_opens() as serial_open_attempts:
        facade = AgentControlFacade(
            log_dir=artifact_dir,
            event_handler=on_event,
            transport_factory_provider=provider,
            serial_open_attempts=serial_open_attempts,
        )

        load_result = facade.load_plan(plan)
        action_results.append(load_result)
        if load_result.ok:
            validate_result = facade.validate_plan()
            action_results.append(validate_result)
        else:
            validate_result = load_result

        if validate_result.ok:
            action_results.append(facade.start_auto_tune())
            action_results.append(facade.stop())
            action_results.append(facade.export_session_report(report))

        report_data = (
            json.loads(report.read_text(encoding="utf-8"))
            if report.exists()
            else {"artifact_paths": {}, "event_counts": {}}
        )
        transport = transport_holder.get("transport")
        probe_summary = _build_probe_summary(
            report=report_data,
            action_results=action_results,
            serial_open_attempts=list(serial_open_attempts),
            artifact_dir=artifact_dir,
            report_path=report,
            summary_path=summary,
            required_event_types=required_event_types,
            control_flags=control_flags,
            virtual_transport_closed=transport.closed if transport is not None else None,
        )

    summary.write_text(json.dumps(probe_summary, ensure_ascii=False, indent=2), encoding="utf-8")
    _validate_probe_summary(probe_summary)
    return probe_summary


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the no-hardware agent console probe against the virtual plan fixture."
    )
    parser.add_argument(
        "--plan",
        type=Path,
        default=DEFAULT_VIRTUAL_PLAN_PATH,
        help="YAML plan to probe. Defaults to the test-only virtual fixture.",
    )
    parser.add_argument(
        "--log-dir",
        type=Path,
        default=None,
        help="Directory for JSONL logs and probe artifacts.",
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=None,
        help="Path for the exported agent report JSON.",
    )
    parser.add_argument(
        "--summary",
        type=Path,
        default=None,
        help="Path for the probe summary JSON.",
    )
    parser.add_argument(
        "--require-event",
        action="append",
        default=[],
        help="Additional structured event type that must appear at least once.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print the probe summary JSON to stdout.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)
    try:
        summary = run_probe(
            plan_path=args.plan,
            log_dir=args.log_dir,
            report_path=args.report,
            summary_path=args.summary,
            extra_required_event_types=list(args.require_event),
        )
    except ProbeFailure as exc:
        print(f"PROBE: failed: {exc}")
        return 1
    except Exception as exc:
        print(f"PROBE: failed: {type(exc).__name__}: {exc}")
        return 1

    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print(
            "PROBE: ok "
            f"report={summary['report_path']} "
            f"summary={summary['summary_path']} "
            f"real_serial_status={summary['real_serial_status']}"
        )
    return 0


__all__ = [
    "AgentControlFacade",
    "ProbeFailure",
    "validate_session_report_schema",
    "run_probe",
    "main",
]


if __name__ == "__main__":
    raise SystemExit(main())
