#!/usr/bin/env python
"""Agent-facing control facade for TuningSession.

The standalone probe CLI is added by a later story. This module intentionally
keeps the first agent surface as a narrow Python API over validated
TuningSession controls.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any, Callable

from tuning_session import (
    ActionResult,
    EventHandler,
    ExecutionError,
    TransportFactory,
    TuningEvent,
    TuningSession,
    serial,
)
from validate_plan import load_plan as _load_plan
from validate_plan import validate_plan as _validate_plan


TransportFactoryProvider = Callable[[dict[str, Any]], TransportFactory]


class AgentControlFacade:
    def __init__(
        self,
        *,
        log_dir: Path | None = None,
        event_handler: EventHandler | None = None,
        transport_factory_provider: TransportFactoryProvider | None = None,
    ) -> None:
        self._log_dir = log_dir
        self._event_handler = event_handler
        self._transport_factory_provider = transport_factory_provider
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
        report = self._build_report()
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        self._last_report_path = path
        return self._record_result(
            ActionResult(
                ok=True,
                action="export_session_report",
                state=self.state,
                message="Session report exported.",
                data={"report_path": str(path)},
            )
        )

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

    def _build_report(self) -> dict[str, Any]:
        session = self._session
        artifact_paths: dict[str, str] = {}
        if session is not None and session.log_path is not None:
            artifact_paths["session_log"] = str(session.log_path)
        if self._last_report_path is not None:
            artifact_paths["last_report"] = str(self._last_report_path)

        event_records = [event.to_record() for event in self._events]
        event_counts = Counter(record["type"] for record in event_records)
        return {
            "plan_path": str(self._plan_path) if self._plan_path is not None else None,
            "validated": self._validated,
            "session_id": session.session_id if session is not None else None,
            "state": self.state,
            "last_exit_code": self._last_exit_code,
            "action_log": list(self._action_log),
            "event_counts": dict(sorted(event_counts.items())),
            "events": event_records,
            "final_summary": session.summary() if session is not None else None,
            "artifact_paths": artifact_paths,
        }


__all__ = ["AgentControlFacade"]
