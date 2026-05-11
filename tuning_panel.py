#!/usr/bin/env python
"""桌面版 MCU 蓝牙调参执行与观测面板。"""

from __future__ import annotations

import argparse
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import tkinter as tk
from tkinter import filedialog, font as tkfont, messagebox, ttk

from serial.tools import list_ports

try:
    from validate_plan import load_plan, validate_plan
except ImportError as exc:  # pragma: no cover - script layout dependent
    print("ERROR: validate_plan.py must be in the same scripts directory.", file=sys.stderr)
    raise SystemExit(3) from exc

try:
    from tuning_session import PySerialTransport, TuningEvent, TuningSession, format_template
except ImportError as exc:  # pragma: no cover - script layout dependent
    print("ERROR: tuning_session.py must be in the same scripts directory.", file=sys.stderr)
    raise SystemExit(3) from exc


SCRIPT_DIR = Path(__file__).resolve().parent
RUN_SCRIPT = SCRIPT_DIR / "run_tuning_plan.py"
MONITOR_SCRIPT = SCRIPT_DIR / "monitor_tuning_plan.py"
SESSION_EVENT = "__SESSION_EVENT__"
SESSION_DONE = "__SESSION_DONE__"
SESSION_ERROR = "__SESSION_ERROR__"


TransportFactory = Callable[[dict[str, Any]], Any]
SESSION_REPLAY_SCHEMA_VERSION = 1


@dataclass
class SessionReplayLoadResult:
    source_path: Path
    session_id: str = ""
    plan_path: str | None = None
    jsonl_path: str | None = None
    transcript_path: str | None = None
    plan_snapshot_path: str | None = None
    summary_path: str | None = None
    final_summary: dict[str, Any] = field(default_factory=dict)
    records: list[dict[str, Any]] = field(default_factory=list)
    event_counts: dict[str, int] = field(default_factory=dict)
    parameter_differences: list[dict[str, Any]] = field(default_factory=list)
    load_errors: list[str] = field(default_factory=list)
    recoverable: bool = True


def _resolve_replay_path(base_path: Path, raw_path: Any) -> str | None:
    if raw_path in {None, ""}:
        return None
    path = Path(str(raw_path))
    if not path.is_absolute():
        path = base_path.parent / path
    return str(path)


def _read_replay_json(path: Path, result: SessionReplayLoadResult) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        result.load_errors.append(f"{path}: {exc}")
        result.recoverable = False
        return None
    except json.JSONDecodeError as exc:
        result.load_errors.append(f"{path}: invalid JSON at line {exc.lineno}: {exc.msg}")
        return None
    if not isinstance(payload, dict):
        result.load_errors.append(f"{path}: top-level JSON must be an object")
        return None
    return payload


def _read_replay_jsonl(path: Path, result: SessionReplayLoadResult) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        result.load_errors.append(f"{path}: {exc}")
        result.recoverable = False
        return records
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            result.load_errors.append(f"{path}:{line_number}: invalid JSON: {exc.msg}")
            continue
        if isinstance(payload, dict):
            records.append(payload)
        else:
            result.load_errors.append(f"{path}:{line_number}: JSONL record must be an object")
    return records


def _summary_from_records(records: list[dict[str, Any]]) -> dict[str, Any]:
    for record in reversed(records):
        summary = record.get("summary")
        if isinstance(summary, dict):
            return dict(summary)
    for record in reversed(records):
        if record.get("type") == "summary" and isinstance(record.get("final_summary"), dict):
            return dict(record["final_summary"])
    return {}


def _first_session_id(records: list[dict[str, Any]]) -> str:
    for record in records:
        session_id = record.get("session_id")
        if session_id:
            return str(session_id)
    return ""


def _baseline_parameters_from_records(records: list[dict[str, Any]]) -> dict[str, Any]:
    for record in records:
        before_params = record.get("before_params")
        if isinstance(before_params, dict):
            return dict(before_params)
    return {}


def _numeric_delta(before: Any, after: Any) -> Any:
    try:
        return float(after) - float(before)
    except (TypeError, ValueError):
        return "-"


def _build_parameter_differences(
    summary: dict[str, Any],
    records: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    final_parameters = summary.get("final_parameters")
    if not isinstance(final_parameters, dict):
        return []
    baseline_parameters = summary.get("baseline_parameters")
    if not isinstance(baseline_parameters, dict):
        baseline_parameters = _baseline_parameters_from_records(records)
    if not isinstance(baseline_parameters, dict) or not baseline_parameters:
        return []

    rows: list[dict[str, Any]] = []
    keys = sorted({str(key) for key in baseline_parameters} | {str(key) for key in final_parameters})
    for key in keys:
        before = baseline_parameters.get(key)
        after = final_parameters.get(key)
        rows.append(
            {
                "key": key,
                "baseline": before,
                "final": after,
                "delta": _numeric_delta(before, after),
            }
        )
    return rows


def _apply_replay_records(
    result: SessionReplayLoadResult,
    records: list[dict[str, Any]],
    *,
    jsonl_path: str | None = None,
) -> None:
    result.records = records
    if jsonl_path is not None:
        result.jsonl_path = jsonl_path
    if not result.final_summary:
        result.final_summary = _summary_from_records(records)
    if not result.session_id:
        result.session_id = (
            str(result.final_summary.get("session_id") or "")
            or _first_session_id(records)
            or (Path(jsonl_path).stem if jsonl_path else "")
        )
    if not result.event_counts:
        result.event_counts = dict(Counter(str(record.get("type")) for record in records if record.get("type")))


def _load_replay_summary_file(path: Path, result: SessionReplayLoadResult) -> None:
    payload = _read_replay_json(path, result)
    if payload is None:
        return
    summary = payload.get("summary") or payload.get("final_summary")
    if isinstance(summary, dict):
        result.final_summary = dict(summary)
    elif any(key in payload for key in ("final_parameters", "baseline_score", "final_score")):
        result.final_summary = dict(payload)
    else:
        result.load_errors.append(f"{path}: missing final summary")
    result.summary_path = str(path)
    if not result.session_id:
        result.session_id = str(payload.get("session_id") or result.final_summary.get("session_id") or path.stem)


def _load_replay_manifest(payload: dict[str, Any], source_path: Path, result: SessionReplayLoadResult) -> None:
    artifact_paths = payload.get("artifact_paths", {})
    if not isinstance(artifact_paths, dict):
        artifact_paths = {}
    result.session_id = str(payload.get("session_id") or "")
    result.plan_path = _resolve_replay_path(source_path, payload.get("plan_path"))
    result.jsonl_path = _resolve_replay_path(
        source_path,
        artifact_paths.get("session_log") or artifact_paths.get("jsonl") or artifact_paths.get("jsonl_path"),
    )
    result.transcript_path = _resolve_replay_path(source_path, artifact_paths.get("transcript"))
    result.plan_snapshot_path = _resolve_replay_path(source_path, artifact_paths.get("plan_snapshot"))
    result.summary_path = _resolve_replay_path(source_path, artifact_paths.get("final_summary") or artifact_paths.get("summary"))

    summary = payload.get("summary") or payload.get("final_summary")
    if isinstance(summary, dict):
        result.final_summary = dict(summary)
    if result.summary_path and Path(result.summary_path).exists() and not result.final_summary:
        _load_replay_summary_file(Path(result.summary_path), result)
    if result.jsonl_path:
        _apply_replay_records(result, _read_replay_jsonl(Path(result.jsonl_path), result), jsonl_path=result.jsonl_path)


def _load_replay_report(payload: dict[str, Any], source_path: Path, result: SessionReplayLoadResult) -> None:
    artifact_paths = payload.get("artifact_paths", {})
    if not isinstance(artifact_paths, dict):
        artifact_paths = {}
    result.session_id = str(payload.get("session_id") or "")
    result.plan_path = _resolve_replay_path(source_path, payload.get("plan_path"))
    result.jsonl_path = _resolve_replay_path(source_path, artifact_paths.get("session_log"))
    result.transcript_path = _resolve_replay_path(source_path, artifact_paths.get("transcript"))
    result.plan_snapshot_path = _resolve_replay_path(source_path, artifact_paths.get("plan_snapshot"))
    result.summary_path = _resolve_replay_path(source_path, artifact_paths.get("final_summary") or artifact_paths.get("summary"))
    result.final_summary = dict(payload.get("final_summary") or {})
    event_counts = payload.get("event_counts")
    if isinstance(event_counts, dict):
        result.event_counts = {str(key): int(value) for key, value in event_counts.items()}
    if result.jsonl_path:
        _apply_replay_records(result, _read_replay_jsonl(Path(result.jsonl_path), result), jsonl_path=result.jsonl_path)


def load_session_replay(source_path: Path | str) -> SessionReplayLoadResult:
    path = Path(source_path)
    result = SessionReplayLoadResult(source_path=path)
    if not path.exists():
        result.load_errors.append(f"{path}: file does not exist")
        result.recoverable = False
        return result

    if path.suffix.lower() == ".jsonl":
        _apply_replay_records(result, _read_replay_jsonl(path, result), jsonl_path=str(path))
    else:
        payload = _read_replay_json(path, result)
        if payload is not None:
            if payload.get("report_type") == "agent_session_report":
                _load_replay_report(payload, path, result)
            elif payload.get("artifact_type") == "tuning_session_manifest" or "artifact_paths" in payload:
                _load_replay_manifest(payload, path, result)
            else:
                _load_replay_summary_file(path, result)

    if not result.final_summary:
        result.load_errors.append("missing final session summary")
    if not result.session_id:
        result.session_id = str(result.final_summary.get("session_id") or path.stem)
    result.parameter_differences = _build_parameter_differences(result.final_summary, result.records)
    if result.final_summary and not result.parameter_differences:
        result.load_errors.append("missing baseline or final parameters for replay comparison")
    return result


def select_execution_backend(mode: str, run_backend: str) -> str:
    if mode == "demo":
        return "demo"
    if mode == "monitor":
        return "subprocess"
    if mode == "run" and run_backend == "session":
        return "session"
    return "subprocess"


class SessionWorker:
    """Run a TuningSession off the Tk main thread and forward events by queue."""

    def __init__(
        self,
        *,
        plan: dict[str, Any],
        plan_path: Path,
        output_queue: queue.Queue[Any],
        log_dir: Path | None = None,
        transport_factory: TransportFactory | None = None,
        active_parameter_keys: list[str] | None = None,
    ) -> None:
        self.output_queue = output_queue
        self.session = TuningSession(
            plan=plan,
            plan_path=plan_path,
            log_dir=log_dir,
            event_handler=self._enqueue_event,
            transport_factory=transport_factory,
        )
        if active_parameter_keys is not None:
            result = self.session.set_active_parameters(list(active_parameter_keys))
            if not result.ok:
                error_code = result.data.get("error_code") or result.data.get("code")
                raise ValueError(f"active parameter selection rejected: {error_code or result.message}")
        self.thread = threading.Thread(target=self._run, name="tuning-session-worker", daemon=True)
        self.exit_code: int | None = None
        self.error: BaseException | None = None

    def _enqueue_event(self, event: TuningEvent) -> None:
        self.output_queue.put((SESSION_EVENT, event))

    def start(self) -> None:
        self.thread.start()

    def is_alive(self) -> bool:
        return self.thread.is_alive()

    def join(self, timeout: float | None = None) -> None:
        self.thread.join(timeout)

    def _run(self) -> None:
        try:
            self.exit_code = self.session.run()
        except BaseException as exc:  # noqa: BLE001 - surface worker failures to the GUI queue
            self.error = exc
            self.exit_code = 1
            self.output_queue.put((SESSION_ERROR, exc))
        finally:
            self.output_queue.put((SESSION_DONE, self.exit_code))


DEMO_LINES = [
    "INFO 演示模式：未打开 COM 口，未发送真实串口命令。",
    "TX STATUS",
    "RX OK kp_x=350 kd_x=30 kp_y=320 kd_y=40",
    "TX START",
    "RX OK",
    "RX 1000,320,300,180,20,20,0.1,5,240,250,-160,-10,18,0.1,-6",
    'DAT {"timestamp":1000,"setpoint_x":320,"input_x":300,"pwm_x":180,"error_x":20,"setpoint_y":240,"input_y":250,"pwm_y":-160,"error_y":-10}',
    "RX 1050,320,308,164,12,18,0.1,4,240,246,-138,-6,17,0.1,-5",
    'DAT {"timestamp":1050,"setpoint_x":320,"input_x":308,"pwm_x":164,"error_x":12,"setpoint_y":240,"input_y":246,"pwm_y":-138,"error_y":-6}',
    "Baseline score: 14.82",
    "第 1 轮: kp_x -> 370",
    "TX SET kp_x 370",
    "RX OK",
    "TX STATUS",
    "RX OK kp_x=370 kd_x=30 kp_y=320 kd_y=40",
    "RX 1200,320,314,145,6,16,0.1,3,240,243,-120,-3,16,0.1,-4",
    'DAT {"timestamp":1200,"setpoint_x":320,"input_x":314,"pwm_x":145,"error_x":6,"setpoint_y":240,"input_y":243,"pwm_y":-120,"error_y":-3}',
    "accept: score=11.93, improvement=19.50%",
    "第 2 轮: kd_x -> 35",
    "TX SET kd_x 35",
    "RX OK",
    "TX STATUS",
    "RX OK kp_x=370 kd_x=35 kp_y=320 kd_y=40",
    "RX 1400,320,330,-190,-10,34,0.1,-12,240,242,-118,-2,15,0.1,-4",
    'DAT {"timestamp":1400,"setpoint_x":320,"input_x":330,"pwm_x":-190,"error_x":-10,"setpoint_y":240,"input_y":242,"pwm_y":-118,"error_y":-2}',
    "rollback: score=16.40, failures=[], rollback=ok",
    "TX SET kd_x 30",
    "RX OK",
    "TX STOP",
    "RX OK",
    'Summary: {"final_parameters":{"kp_x":370,"kd_x":30,"kp_y":320,"kd_y":40},"baseline_score":14.82,"final_score":11.93,"accepted":1,"rolled_back":1,"stop_reason":"demo_complete"}',
]


class TuningPanel(tk.Tk):
    def __init__(
        self,
        auto_demo: bool = False,
        *,
        run_backend: str | None = None,
        session_transport_factory: TransportFactory | None = None,
        ui_mode: str | None = None,
    ) -> None:
        super().__init__()
        self.ui_mode = ui_mode or os.environ.get("MCU_TUNING_PANEL_UI", "simple")
        if self.ui_mode not in {"simple", "advanced"}:
            self.ui_mode = "simple"
        self.title("MCU 调参控制台" if self.ui_mode == "simple" else "MCU 蓝牙调参面板")
        self.geometry("1120x720" if self.ui_mode == "simple" else "1180x760")
        self.minsize(980, 640)

        self.plan_path = tk.StringVar()
        self.mode = tk.StringVar(value="demo")
        self.status = tk.StringVar(value="空闲")
        self.port = tk.StringVar(value="-")
        self.baudrate = tk.StringVar(value="-")
        self.connection_state = tk.StringVar(value="未连接")
        self.detected_ports = tk.StringVar(value="未检测")
        self.connection_detail = tk.StringVar(value="未加载计划")
        self.score = tk.StringVar(value="-")
        self.decision = tk.StringVar(value="-")
        self.transcript_path = tk.StringVar(value="-")
        self.current_round = tk.StringVar(value="-")
        self.accepted_count = tk.StringVar(value="0")
        self.held_count = tk.StringVar(value="0")
        self.rollback_count = tk.StringVar(value="0")
        self.failed_count = tk.StringVar(value="0")
        self.stop_reason = tk.StringVar(value="-")
        self.latest_event = tk.StringVar(value="-")
        self.plan_status = tk.StringVar(value="未加载")
        self.parameter_selection_status = tk.StringVar(value="未加载参数")
        self.parameter_axis = tk.StringVar(value="")
        self.parameter_group = tk.StringVar(value="")
        self.parameter_keys_text = tk.StringVar(value="")
        self.safety_status = tk.StringVar(value="未加载安全规则")
        self.command_preview_status = tk.StringVar(value="未加载命令预览")
        self.replay_path = tk.StringVar(value="")
        self.replay_status = tk.StringVar(value="未加载回放")

        self.plan: dict[str, Any] | None = None
        self.validated_plan_path: Path | None = None
        self.session: Any | None = None
        self.session_worker: SessionWorker | None = None
        self.operator_buttons: dict[str, ttk.Button] = {}
        self.connection_buttons: dict[str, ttk.Button] = {}
        self.parameter_metadata_by_key: dict[str, dict[str, Any]] = {}
        self.parameter_current_values: dict[str, Any] = {}
        self.parameter_baseline_values: dict[str, Any] = {}
        self.parameter_last_stable_values: dict[str, Any] = {}
        self.parameter_trial_values: dict[str, Any] = {}
        self.parameter_active_keys: list[str] | None = None
        self.parameter_skipped_keys: set[str] = set()
        self.safety_triggered_rules: set[str] = set()
        self.command_preview: dict[str, str] = {"source": "-", "command": "-", "detail": "未加载 YAML 计划"}
        self.last_sent_command: dict[str, str] = {"source": "-", "command": "-", "detail": "尚未发送"}
        self.current_session_artifacts: dict[str, str] = {}
        self.replay_data: SessionReplayLoadResult | None = None
        self.connection_transport: Any | None = None
        self.session_transport_factory = session_transport_factory
        self.run_backend = run_backend or os.environ.get("MCU_TUNING_PANEL_RUN_BACKEND", "session")
        if self.run_backend not in {"session", "subprocess"}:
            self.run_backend = "session"
        self.proc: subprocess.Popen[str] | None = None
        self.reader_thread: threading.Thread | None = None
        self.output_queue: queue.Queue[Any] = queue.Queue()
        self.stop_requested = False
        self.demo_index = 0
        self.waveform_samples: list[dict[str, float]] = []
        self.transcript_handle: Any | None = None
        self.ui_font = self._pick_font(("Microsoft YaHei UI", "Segoe UI", "Arial"))
        self.mono_font = self._pick_font(("Cascadia Mono", "Consolas", "Courier New"))
        self.colors = {
            "bg": "#edf1ee",
            "panel": "#fbfcfa",
            "panel_soft": "#f4f7f5",
            "header": "#141a18",
            "header_subtle": "#aebdb7",
            "text": "#15201d",
            "muted": "#66736f",
            "border": "#d5ddd8",
            "accent": "#087f8c",
            "accent_active": "#0f6b73",
            "danger": "#c2410c",
            "danger_active": "#9a3412",
            "warning": "#d97706",
            "success": "#2f9e44",
            "rail": "#18211f",
            "rail_soft": "#22302d",
            "log_bg": "#111715",
            "log_fg": "#d9e3df",
        }
        self._configure_fonts()

        self._build_style()
        self._build_ui()
        self.after(80, self._pump_output)

        if auto_demo:
            self.after(300, self.start)

    def _pick_font(self, candidates: tuple[str, ...]) -> str:
        available = {name.lower(): name for name in tkfont.families(self)}
        for candidate in candidates:
            if candidate.lower() in available:
                return available[candidate.lower()]
        return candidates[-1]

    def _configure_fonts(self) -> None:
        font_specs = {
            "TkDefaultFont": (10, "normal"),
            "TkTextFont": (10, "normal"),
            "TkMenuFont": (10, "normal"),
            "TkCaptionFont": (10, "normal"),
            "TkSmallCaptionFont": (9, "normal"),
            "TkIconFont": (10, "normal"),
            "TkTooltipFont": (9, "normal"),
            "TkHeadingFont": (10, "bold"),
        }
        for font_name, (size, weight) in font_specs.items():
            try:
                tkfont.nametofont(font_name).configure(family=self.ui_font, size=size, weight=weight)
            except tk.TclError:
                continue

    def _build_style(self) -> None:
        self.configure(bg=self.colors["bg"])
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("TFrame", background=self.colors["bg"])
        style.configure("Card.TFrame", background=self.colors["panel"], relief="flat")
        style.configure("Surface.TFrame", background=self.colors["panel_soft"], relief="flat")
        style.configure("Rail.TFrame", background=self.colors["rail"], relief="flat")
        style.configure("RailSoft.TFrame", background=self.colors["rail_soft"], relief="flat")
        style.configure(
            "Header.TLabel",
            background=self.colors["header"],
            foreground="#ffffff",
            font=(self.ui_font, 18, "bold"),
        )
        style.configure(
            "SubHeader.TLabel",
            background=self.colors["header"],
            foreground=self.colors["header_subtle"],
            font=(self.ui_font, 10),
        )
        style.configure(
            "CardTitle.TLabel",
            background=self.colors["panel"],
            foreground=self.colors["text"],
            font=(self.ui_font, 11, "bold"),
        )
        style.configure(
            "HeroTitle.TLabel",
            background=self.colors["header"],
            foreground="#ffffff",
            font=(self.ui_font, 17, "bold"),
        )
        style.configure(
            "RailTitle.TLabel",
            background=self.colors["rail"],
            foreground="#f8faf8",
            font=(self.ui_font, 13, "bold"),
        )
        style.configure(
            "RailMuted.TLabel",
            background=self.colors["rail"],
            foreground="#aebdb7",
            font=(self.ui_font, 9),
        )
        style.configure(
            "RailValue.TLabel",
            background=self.colors["rail_soft"],
            foreground="#f8faf8",
            font=(self.ui_font, 11, "bold"),
        )
        style.configure(
            "RailCaption.TLabel",
            background=self.colors["rail_soft"],
            foreground="#b7c7c1",
            font=(self.ui_font, 9),
        )
        style.configure(
            "Pill.TLabel",
            background="#e6f5f3",
            foreground=self.colors["accent"],
            font=(self.ui_font, 9, "bold"),
            padding=(10, 4),
        )
        style.configure(
            "Metric.TLabel",
            background=self.colors["panel"],
            foreground=self.colors["text"],
            font=(self.ui_font, 17, "bold"),
        )
        style.configure(
            "SmallMetric.TLabel",
            background=self.colors["panel"],
            foreground=self.colors["text"],
            font=(self.ui_font, 13, "bold"),
        )
        style.configure(
            "Muted.TLabel",
            background=self.colors["panel"],
            foreground=self.colors["muted"],
            font=(self.ui_font, 10),
        )
        style.configure("Footer.TLabel", background=self.colors["bg"], foreground=self.colors["muted"], font=(self.ui_font, 9))
        style.configure("FooterValue.TLabel", background=self.colors["bg"], foreground=self.colors["text"], font=(self.ui_font, 9))
        style.configure("TEntry", fieldbackground="#ffffff", bordercolor=self.colors["border"], padding=(8, 5), font=(self.ui_font, 10))
        style.configure("TRadiobutton", background=self.colors["panel"], foreground=self.colors["text"], font=(self.ui_font, 10))
        style.configure("Rail.TRadiobutton", background=self.colors["rail"], foreground="#f8faf8", font=(self.ui_font, 10))
        style.map("TRadiobutton", background=[("active", self.colors["panel"])])
        style.map("Rail.TRadiobutton", background=[("active", self.colors["rail_soft"])], foreground=[("active", "#ffffff")])
        style.configure("TButton", font=(self.ui_font, 10), padding=(12, 7), borderwidth=0)
        style.configure("Accent.TButton", background=self.colors["accent"], foreground="#ffffff", font=(self.ui_font, 10, "bold"))
        style.map("Accent.TButton", background=[("active", self.colors["accent_active"])])
        style.configure("Danger.TButton", background=self.colors["danger"], foreground="#ffffff", font=(self.ui_font, 10, "bold"))
        style.map("Danger.TButton", background=[("active", self.colors["danger_active"])])
        style.configure("Quiet.TButton", background="#e8eee9", foreground=self.colors["text"], font=(self.ui_font, 10), padding=(12, 7))
        style.map("Quiet.TButton", background=[("active", "#dbe6df")])
        style.configure("TNotebook", background=self.colors["bg"], borderwidth=0)
        style.configure(
            "TNotebook.Tab",
            background="#dde6e1",
            foreground="#33413d",
            font=(self.ui_font, 10, "bold"),
            padding=(18, 9),
        )
        style.map(
            "TNotebook.Tab",
            background=[("selected", self.colors["panel"]), ("active", "#eef4f0")],
            foreground=[("selected", self.colors["text"]), ("active", self.colors["text"])],
        )
        style.configure(
            "Treeview",
            rowheight=28,
            font=(self.ui_font, 10),
            background="#ffffff",
            fieldbackground="#ffffff",
            foreground=self.colors["text"],
            borderwidth=0,
        )
        style.configure(
            "Treeview.Heading",
            background=self.colors["panel_soft"],
            foreground="#334155",
            font=(self.ui_font, 10, "bold"),
            padding=(8, 7),
        )
        style.map("Treeview", background=[("selected", "#dbeafe")], foreground=[("selected", self.colors["text"])])

    def _build_ui(self) -> None:
        if self.ui_mode == "advanced":
            self._build_advanced_ui()
            return
        self._build_simple_ui()

    def _build_simple_ui(self) -> None:
        header = tk.Frame(self, bg=self.colors["header"], height=76)
        header.pack(fill="x")
        header.pack_propagate(False)
        header.columnconfigure(0, weight=1)
        title_block = tk.Frame(header, bg=self.colors["header"])
        title_block.grid(row=0, column=0, sticky="w", padx=22, pady=(12, 0))
        ttk.Label(title_block, text="MCU 调参控制台", style="HeroTitle.TLabel").pack(anchor="w")
        ttk.Label(title_block, text="过程、历史、得分与波形集中在一个清爽工作台。", style="SubHeader.TLabel").pack(
            anchor="w",
            pady=(4, 0),
        )
        status_block = tk.Frame(header, bg=self.colors["header"])
        status_block.grid(row=0, column=1, sticky="e", padx=22, pady=(16, 0))
        ttk.Label(status_block, textvariable=self.status, style="Pill.TLabel").pack(side="right")
        ttk.Label(status_block, text="当前状态", style="SubHeader.TLabel").pack(side="right", padx=(0, 10))

        main = ttk.Frame(self, padding=14)
        main.pack(fill="both", expand=True)
        main.columnconfigure(0, minsize=286)
        main.columnconfigure(1, weight=1)
        main.rowconfigure(0, weight=1)

        rail = ttk.Frame(main, style="Rail.TFrame", padding=16)
        rail.grid(row=0, column=0, sticky="nsew", padx=(0, 14))
        rail.columnconfigure(0, weight=1)
        ttk.Label(rail, text="Session", style="RailTitle.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(rail, text="只保留启动、校验和人工干预。", style="RailMuted.TLabel").grid(
            row=1,
            column=0,
            sticky="w",
            pady=(4, 14),
        )

        plan_card = ttk.Frame(rail, style="RailSoft.TFrame", padding=12)
        plan_card.grid(row=2, column=0, sticky="ew", pady=(0, 12))
        plan_card.columnconfigure(0, weight=1)
        ttk.Label(plan_card, text="调参计划", style="RailCaption.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Entry(plan_card, textvariable=self.plan_path).grid(row=1, column=0, columnspan=2, sticky="ew", pady=(8, 8))
        ttk.Button(plan_card, text="选择 YAML", command=self.browse_plan, style="Quiet.TButton").grid(
            row=2,
            column=0,
            sticky="ew",
            padx=(0, 6),
        )
        ttk.Button(plan_card, text="校验", command=self.validate_current_plan, style="Accent.TButton").grid(
            row=2,
            column=1,
            sticky="ew",
        )

        mode_card = ttk.Frame(rail, style="Rail.TFrame")
        mode_card.grid(row=3, column=0, sticky="ew", pady=(0, 12))
        ttk.Label(mode_card, text="运行模式", style="RailMuted.TLabel").pack(anchor="w", pady=(0, 8))
        ttk.Radiobutton(mode_card, text="演示", variable=self.mode, value="demo", style="Rail.TRadiobutton").pack(anchor="w", pady=(0, 6))
        ttk.Radiobutton(mode_card, text="观测", variable=self.mode, value="monitor", style="Rail.TRadiobutton").pack(anchor="w", pady=(0, 6))
        ttk.Radiobutton(mode_card, text="自动调参", variable=self.mode, value="run", style="Rail.TRadiobutton").pack(anchor="w")

        action_card = ttk.Frame(rail, style="RailSoft.TFrame", padding=12)
        action_card.grid(row=4, column=0, sticky="ew", pady=(0, 12))
        ttk.Label(action_card, text="动作", style="RailCaption.TLabel").pack(anchor="w", pady=(0, 8))
        primary_actions = ttk.Frame(action_card, style="RailSoft.TFrame")
        primary_actions.pack(fill="x")
        self._add_operator_button(primary_actions, "start_monitor", "开始观测", self.start_monitor, "Accent.TButton")
        self.operator_buttons["start_monitor"].pack_configure(side="top", fill="x", pady=(0, 8), padx=0)
        self._add_operator_button(primary_actions, "start_auto_tune", "开始调参", self.start_auto_tune, "Accent.TButton")
        self.operator_buttons["start_auto_tune"].pack_configure(side="top", fill="x", pady=(0, 10), padx=0)

        sub_actions = ttk.Frame(action_card, style="RailSoft.TFrame")
        sub_actions.pack(fill="x")
        sub_actions.columnconfigure((0, 1), weight=1)
        self.operator_buttons["pause"] = ttk.Button(sub_actions, text="暂停", command=self.pause_session, style="Quiet.TButton")
        self.operator_buttons["pause"].grid(row=0, column=0, sticky="ew", padx=(0, 6), pady=(0, 8))
        self.operator_buttons["resume"] = ttk.Button(sub_actions, text="继续", command=self.resume_session, style="Quiet.TButton")
        self.operator_buttons["resume"].grid(row=0, column=1, sticky="ew", pady=(0, 8))
        self.operator_buttons["stop"] = ttk.Button(sub_actions, text="停止", command=self.stop, style="Danger.TButton")
        self.operator_buttons["stop"].grid(row=1, column=0, sticky="ew", padx=(0, 6))
        self.operator_buttons["emergency_stop"] = ttk.Button(
            sub_actions,
            text="急停",
            command=self.emergency_stop,
            style="Danger.TButton",
        )
        self.operator_buttons["emergency_stop"].grid(row=1, column=1, sticky="ew")

        info_card = ttk.Frame(rail, style="RailSoft.TFrame", padding=12)
        info_card.grid(row=5, column=0, sticky="ew")
        info_card.columnconfigure(0, weight=1)
        for row, (caption, variable) in enumerate(
            (
                ("串口", self.port),
                ("波特率", self.baudrate),
                ("记录", self.transcript_path),
            )
        ):
            ttk.Label(info_card, text=caption, style="RailCaption.TLabel").grid(row=row * 2, column=0, sticky="w")
            ttk.Label(info_card, textvariable=variable, style="RailValue.TLabel", wraplength=220).grid(
                row=row * 2 + 1,
                column=0,
                sticky="ew",
                pady=(3, 10 if row < 2 else 0),
            )
        rail.rowconfigure(6, weight=1)
        ttk.Button(rail, text="打开记录", command=self.open_transcript, style="Quiet.TButton").grid(
            row=7,
            column=0,
            sticky="ew",
            pady=(14, 0),
        )

        self.notebook = ttk.Notebook(main)
        self.notebook.grid(row=0, column=1, sticky="nsew")
        self._build_simple_tuning_page()
        self._build_history_page()
        self._build_waveform_page()

        self._refresh_operator_controls()

    def _build_simple_tuning_page(self) -> None:
        page = ttk.Frame(self.notebook, padding=12)
        self.notebook.add(page, text="调参")
        page.columnconfigure(0, weight=3)
        page.columnconfigure(1, weight=2)
        page.rowconfigure(1, weight=1)
        page.rowconfigure(2, weight=1)

        metrics = ttk.Frame(page)
        metrics.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 12))
        for column in range(6):
            metrics.columnconfigure(column, weight=1)
        self._small_metric_card(metrics, "状态", self.status, 0, 0)
        self._small_metric_card(metrics, "轮次", self.current_round, 0, 1)
        self._small_metric_card(metrics, "评分", self.score, 0, 2)
        self._small_metric_card(metrics, "决策", self.decision, 0, 3)
        self._small_metric_card(metrics, "接受", self.accepted_count, 0, 4)
        self._small_metric_card(metrics, "回滚", self.rollback_count, 0, 5)

        tuning_card = ttk.Frame(page, style="Card.TFrame", padding=12)
        tuning_card.grid(row=1, column=0, sticky="nsew", padx=(0, 12), pady=(0, 12))
        tuning_card.rowconfigure(1, weight=1)
        tuning_card.columnconfigure(0, weight=1)
        ttk.Label(tuning_card, text="调参过程", style="CardTitle.TLabel").grid(row=0, column=0, sticky="w")
        self.tuning_tree = ttk.Treeview(tuning_card, columns=("value",), show="tree headings", height=9)
        self.tuning_tree.heading("#0", text="项目")
        self.tuning_tree.heading("value", text="值")
        self.tuning_tree.column("#0", width=170)
        self.tuning_tree.column("value", width=470)
        self.tuning_tree.tag_configure("oddrow", background=self.colors["panel_soft"])
        self.tuning_tree.tag_configure("evenrow", background="#ffffff")
        self.tuning_tree.grid(row=1, column=0, sticky="nsew", pady=(10, 0))

        log_card = ttk.Frame(page, style="Card.TFrame", padding=12)
        log_card.grid(row=2, column=0, sticky="nsew", padx=(0, 12))
        log_card.rowconfigure(1, weight=1)
        log_card.columnconfigure(0, weight=1)
        ttk.Label(log_card, text="上下行", style="CardTitle.TLabel").grid(row=0, column=0, sticky="w")
        self.log = tk.Text(
            log_card,
            wrap="none",
            height=10,
            bg=self.colors["log_bg"],
            fg=self.colors["log_fg"],
            insertbackground="#ffffff",
            font=(self.mono_font, 10),
            relief="flat",
            bd=0,
            padx=10,
            pady=8,
            spacing1=1,
            spacing3=1,
            selectbackground="#334155",
            selectforeground="#ffffff",
            highlightthickness=1,
            highlightbackground="#243044",
            highlightcolor="#3b82f6",
        )
        self.log.grid(row=1, column=0, sticky="nsew", pady=(10, 0))
        log_scroll = ttk.Scrollbar(log_card, orient="vertical", command=self.log.yview)
        log_scroll.grid(row=1, column=1, sticky="ns", pady=(10, 0))
        self.log.configure(yscrollcommand=log_scroll.set)
        self._configure_log_tags()

        param_card = ttk.Frame(page, style="Card.TFrame", padding=12)
        param_card.grid(row=1, column=1, sticky="nsew", pady=(0, 12))
        param_card.rowconfigure(1, weight=1)
        param_card.columnconfigure(0, weight=1)
        ttk.Label(param_card, text="参数", style="CardTitle.TLabel").grid(row=0, column=0, sticky="w")
        self.param_tree = ttk.Treeview(param_card, columns=("key", "current", "range", "role"), show="headings", height=8)
        self.param_tree.heading("key", text="参数")
        self.param_tree.heading("current", text="当前")
        self.param_tree.heading("range", text="范围")
        self.param_tree.heading("role", text="角色")
        self.param_tree.column("key", width=90, anchor="center")
        self.param_tree.column("current", width=80, anchor="center")
        self.param_tree.column("range", width=105, anchor="center")
        self.param_tree.column("role", width=80, anchor="center")
        self.param_tree.tag_configure("oddrow", background=self.colors["panel_soft"])
        self.param_tree.tag_configure("evenrow", background="#ffffff")
        self.param_tree.grid(row=1, column=0, sticky="nsew", pady=(10, 0))

        data_card = ttk.Frame(page, style="Card.TFrame", padding=12)
        data_card.grid(row=2, column=1, sticky="nsew")
        data_card.rowconfigure(1, weight=1)
        data_card.columnconfigure(0, weight=1)
        ttk.Label(data_card, text="最新 DAT", style="CardTitle.TLabel").grid(row=0, column=0, sticky="w")
        self.dat_tree = ttk.Treeview(data_card, columns=("value",), show="tree headings", height=6)
        self.dat_tree.heading("#0", text="字段")
        self.dat_tree.heading("value", text="值")
        self.dat_tree.column("#0", width=160)
        self.dat_tree.column("value", width=180)
        self.dat_tree.tag_configure("oddrow", background=self.colors["panel_soft"])
        self.dat_tree.tag_configure("evenrow", background="#ffffff")
        self.dat_tree.grid(row=1, column=0, sticky="nsew", pady=(10, 0))

    def _build_waveform_page(self) -> None:
        page = ttk.Frame(self.notebook, padding=12)
        self.notebook.add(page, text="波形")
        page.columnconfigure(0, weight=1)
        page.rowconfigure(0, weight=1)

        chart_card = ttk.Frame(page, style="Card.TFrame", padding=12)
        chart_card.grid(row=0, column=0, sticky="nsew")
        chart_card.rowconfigure(1, weight=1)
        chart_card.columnconfigure(0, weight=1)
        ttk.Label(chart_card, text="波形图", style="CardTitle.TLabel").grid(row=0, column=0, sticky="w")
        self.waveform_canvas = tk.Canvas(
            chart_card,
            bg=self.colors["log_bg"],
            highlightthickness=1,
            highlightbackground="#293733",
        )
        self.waveform_canvas.grid(row=1, column=0, sticky="nsew", pady=(10, 0))
        self.waveform_canvas.bind("<Configure>", lambda _event: self._draw_waveform())
        self._draw_waveform()

    def _build_advanced_ui(self) -> None:
        header = tk.Frame(self, bg=self.colors["header"], height=86)
        header.pack(fill="x")
        header.pack_propagate(False)
        ttk.Label(header, text="MCU 蓝牙调参面板", style="Header.TLabel").pack(anchor="w", padx=24, pady=(16, 0))
        ttk.Label(
            header,
            text="YAML 校验、实时 TX/RX/DAT 观测、自动调参执行和记录归档。",
            style="SubHeader.TLabel",
        ).pack(anchor="w", padx=24, pady=(6, 0))

        main = ttk.Frame(self, padding=16)
        main.pack(fill="both", expand=True)
        main.columnconfigure(0, weight=1)
        main.rowconfigure(1, weight=1)

        controls = ttk.Frame(main, style="Card.TFrame", padding=16)
        controls.grid(row=0, column=0, sticky="ew", pady=(0, 14))
        controls.columnconfigure(1, weight=1)

        ttk.Label(controls, text="调参计划 YAML", style="CardTitle.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Entry(controls, textvariable=self.plan_path).grid(row=0, column=1, sticky="ew", padx=10)
        ttk.Button(controls, text="选择文件", command=self.browse_plan).grid(row=0, column=2, padx=(0, 8))
        ttk.Button(controls, text="校验", command=self.validate_current_plan).grid(row=0, column=3, padx=(0, 8))
        ttk.Button(controls, text="启动所选模式", style="Accent.TButton", command=self.start).grid(
            row=0,
            column=4,
            padx=(0, 8),
        )
        ttk.Button(controls, text="清空日志", command=self.clear_log).grid(row=0, column=5, padx=(0, 8))
        ttk.Button(controls, text="打开记录", command=self.open_transcript).grid(row=0, column=6)

        mode_frame = ttk.Frame(controls, style="Card.TFrame")
        mode_frame.grid(row=1, column=1, sticky="w", padx=8, pady=(10, 0))
        ttk.Radiobutton(mode_frame, text="演示", variable=self.mode, value="demo").pack(side="left", padx=(0, 14))
        ttk.Radiobutton(mode_frame, text="观测", variable=self.mode, value="monitor").pack(side="left", padx=(0, 14))
        ttk.Radiobutton(mode_frame, text="自动调参", variable=self.mode, value="run").pack(side="left")

        operator_frame = ttk.Frame(controls, style="Card.TFrame")
        operator_frame.grid(row=2, column=0, columnspan=7, sticky="w", pady=(12, 0))
        self._add_operator_button(operator_frame, "start_monitor", "启动观测", self.start_monitor, "Accent.TButton")
        self._add_operator_button(operator_frame, "start_auto_tune", "启动自动调参", self.start_auto_tune, "Accent.TButton")
        self._add_operator_button(operator_frame, "pause", "暂停", self.pause_session)
        self._add_operator_button(operator_frame, "resume", "恢复", self.resume_session)
        self._add_operator_button(operator_frame, "skip_current_param", "跳过当前参数", self.skip_current_parameter)
        self._add_operator_button(operator_frame, "rollback_last_stable", "回滚到稳定值", self.rollback_to_last_stable)
        self._add_operator_button(operator_frame, "rollback_baseline", "回滚到基线", self.rollback_to_baseline)
        self._add_operator_button(operator_frame, "stop", "停止", self.stop, "Danger.TButton")
        self._add_operator_button(operator_frame, "emergency_stop", "急停", self.emergency_stop, "Danger.TButton")

        self.notebook = ttk.Notebook(main)
        self.notebook.grid(row=1, column=0, sticky="nsew")

        self._build_overview_page()
        self._build_connection_page()
        self._build_parameter_page()
        self._build_safety_page()
        self._build_plan_page()
        self._build_monitor_page()
        self._build_tuning_page()
        self._build_replay_page()
        self._build_history_page()

        footer = ttk.Frame(self, padding=(16, 0, 16, 12))
        footer.pack(fill="x")
        ttk.Label(footer, text="记录文件:", style="Footer.TLabel").pack(side="left")
        ttk.Label(footer, textvariable=self.transcript_path, style="FooterValue.TLabel").pack(side="left", padx=(6, 0))
        self._refresh_operator_controls()

    def _add_operator_button(
        self,
        parent: ttk.Frame,
        name: str,
        text: str,
        command: Callable[[], Any],
        style: str | None = None,
    ) -> None:
        button = ttk.Button(parent, text=text, command=command, style=style)
        button.pack(side="left", padx=(0, 8))
        self.operator_buttons[name] = button

    def _add_connection_button(
        self,
        parent: ttk.Frame,
        name: str,
        text: str,
        command: Callable[[], Any],
        style: str | None = None,
    ) -> None:
        button = ttk.Button(parent, text=text, command=command, style=style)
        button.pack(side="left", padx=(0, 8))
        self.connection_buttons[name] = button

    def _build_overview_page(self) -> None:
        page = ttk.Frame(self.notebook, padding=14)
        self.notebook.add(page, text="总览")
        page.columnconfigure(0, weight=0, minsize=320)
        page.columnconfigure(1, weight=1)
        page.rowconfigure(0, weight=1)

        sidebar = ttk.Frame(page)
        sidebar.grid(row=0, column=0, sticky="nsew", padx=(0, 14))
        sidebar.columnconfigure(0, weight=1)

        self._metric_card(sidebar, "状态", self.status, 0)
        self._metric_card(sidebar, "串口", self.port, 1)
        self._metric_card(sidebar, "波特率", self.baudrate, 2)
        self._metric_card(sidebar, "评分", self.score, 3)
        self._metric_card(sidebar, "决策", self.decision, 4)
        self._metric_card(sidebar, "最新事件", self.latest_event, 5)

        params_card = ttk.Frame(page, style="Card.TFrame", padding=12)
        params_card.grid(row=0, column=1, sticky="nsew")
        params_card.rowconfigure(1, weight=1)
        params_card.columnconfigure(0, weight=1)
        ttk.Label(params_card, text="参数", style="CardTitle.TLabel").pack(anchor="w")
        self.param_tree = ttk.Treeview(params_card, columns=("key", "current", "range", "role"), show="headings", height=8)
        self.param_tree.heading("key", text="参数")
        self.param_tree.heading("current", text="当前值")
        self.param_tree.heading("range", text="范围")
        self.param_tree.heading("role", text="角色")
        self.param_tree.column("key", width=80, anchor="center")
        self.param_tree.column("current", width=90, anchor="center")
        self.param_tree.column("range", width=110, anchor="center")
        self.param_tree.column("role", width=90, anchor="center")
        self.param_tree.tag_configure("oddrow", background=self.colors["panel_soft"])
        self.param_tree.tag_configure("evenrow", background="#ffffff")
        self.param_tree.pack(fill="both", expand=True, pady=(10, 0))

    def _build_connection_page(self) -> None:
        page = ttk.Frame(self.notebook, padding=14)
        self.notebook.add(page, text="连接")
        page.columnconfigure(0, weight=0, minsize=320)
        page.columnconfigure(1, weight=1)
        page.rowconfigure(0, weight=1)

        sidebar = ttk.Frame(page)
        sidebar.grid(row=0, column=0, sticky="nsew", padx=(0, 14))
        sidebar.columnconfigure(0, weight=1)

        self._metric_card(sidebar, "连接状态", self.connection_state, 0)
        self._metric_card(sidebar, "YAML 串口", self.port, 1)
        self._metric_card(sidebar, "YAML 波特率", self.baudrate, 2)
        self._metric_card(sidebar, "检测端口", self.detected_ports, 3)

        control_card = ttk.Frame(sidebar, style="Card.TFrame", padding=12)
        control_card.grid(row=4, column=0, sticky="ew", pady=(0, 10))
        ttk.Label(control_card, text="连接控制", style="CardTitle.TLabel").pack(anchor="w")
        ttk.Label(control_card, textvariable=self.connection_detail, style="Muted.TLabel").pack(anchor="w", pady=(6, 10))
        buttons = ttk.Frame(control_card, style="Card.TFrame")
        buttons.pack(anchor="w")
        self._add_connection_button(buttons, "detect_ports", "检测端口", self.detect_ports_action)
        self._add_connection_button(buttons, "connect", "连接", self.connect_transport, "Accent.TButton")
        self._add_connection_button(buttons, "disconnect", "断开", self.disconnect_transport, "Danger.TButton")

        metadata_card = ttk.Frame(page, style="Card.TFrame", padding=12)
        metadata_card.grid(row=0, column=1, sticky="nsew")
        metadata_card.rowconfigure(1, weight=1)
        metadata_card.columnconfigure(0, weight=1)
        ttk.Label(metadata_card, text="YAML transport 元数据", style="CardTitle.TLabel").grid(row=0, column=0, sticky="w")
        self.connection_tree = ttk.Treeview(metadata_card, columns=("value",), show="tree headings", height=14)
        self.connection_tree.heading("#0", text="项目")
        self.connection_tree.heading("value", text="值")
        self.connection_tree.column("#0", width=260)
        self.connection_tree.column("value", width=520)
        self.connection_tree.tag_configure("oddrow", background=self.colors["panel_soft"])
        self.connection_tree.tag_configure("evenrow", background="#ffffff")
        self.connection_tree.grid(row=1, column=0, sticky="nsew", pady=(10, 0))
        scroll = ttk.Scrollbar(metadata_card, orient="vertical", command=self.connection_tree.yview)
        scroll.grid(row=1, column=1, sticky="ns", pady=(10, 0))
        self.connection_tree.configure(yscrollcommand=scroll.set)
        self._populate_connection_tree(None)
        self._refresh_connection_controls()

    def _build_parameter_page(self) -> None:
        page = ttk.Frame(self.notebook, padding=14)
        self.notebook.add(page, text="参数")
        page.columnconfigure(0, weight=1)
        page.rowconfigure(1, weight=1)

        controls = ttk.Frame(page, style="Card.TFrame", padding=12)
        controls.grid(row=0, column=0, sticky="ew", pady=(0, 14))
        controls.columnconfigure(7, weight=1)
        ttk.Label(controls, text="参数参与选择", style="CardTitle.TLabel").grid(row=0, column=0, sticky="w", padx=(0, 12))

        ttk.Label(controls, text="轴", style="Muted.TLabel").grid(row=0, column=1, sticky="e")
        self.parameter_axis_combo = ttk.Combobox(
            controls,
            textvariable=self.parameter_axis,
            values=[],
            state="readonly",
            width=12,
        )
        self.parameter_axis_combo.grid(row=0, column=2, sticky="w", padx=(6, 8))
        ttk.Button(controls, text="应用轴", command=self.apply_axis_parameter_selection).grid(row=0, column=3, padx=(0, 12))

        ttk.Label(controls, text="分组", style="Muted.TLabel").grid(row=0, column=4, sticky="e")
        self.parameter_group_combo = ttk.Combobox(
            controls,
            textvariable=self.parameter_group,
            values=[],
            state="readonly",
            width=16,
        )
        self.parameter_group_combo.grid(row=0, column=5, sticky="w", padx=(6, 8))
        ttk.Button(controls, text="应用分组", command=self.apply_group_parameter_selection).grid(row=0, column=6, padx=(0, 12))
        ttk.Button(controls, text="选择全部", command=self.select_all_parameters).grid(row=0, column=7, sticky="w")

        specific = ttk.Frame(controls, style="Card.TFrame")
        specific.grid(row=1, column=0, columnspan=8, sticky="ew", pady=(10, 0))
        specific.columnconfigure(1, weight=1)
        ttk.Label(specific, text="指定参数", style="Muted.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Entry(specific, textvariable=self.parameter_keys_text).grid(row=0, column=1, sticky="ew", padx=(8, 8))
        ttk.Button(specific, text="应用指定键", command=self.apply_specific_parameter_selection).grid(row=0, column=2)
        ttk.Label(specific, textvariable=self.parameter_selection_status, style="Muted.TLabel").grid(
            row=1,
            column=0,
            columnspan=3,
            sticky="w",
            pady=(8, 0),
        )

        table_card = ttk.Frame(page, style="Card.TFrame", padding=12)
        table_card.grid(row=1, column=0, sticky="nsew")
        table_card.rowconfigure(1, weight=1)
        table_card.columnconfigure(0, weight=1)
        ttk.Label(table_card, text="参数运行视图", style="CardTitle.TLabel").grid(row=0, column=0, sticky="w")
        self.parameter_tree = ttk.Treeview(
            table_card,
            columns=("key", "current", "baseline", "last_stable", "trial", "range", "step", "participation"),
            show="headings",
            height=14,
        )
        headings = {
            "key": "参数",
            "current": "当前值",
            "baseline": "基线",
            "last_stable": "稳定值",
            "trial": "试验值",
            "range": "范围",
            "step": "步长",
            "participation": "参与状态",
        }
        widths = {
            "key": 100,
            "current": 95,
            "baseline": 95,
            "last_stable": 95,
            "trial": 95,
            "range": 130,
            "step": 130,
            "participation": 100,
        }
        for column, heading in headings.items():
            self.parameter_tree.heading(column, text=heading)
            self.parameter_tree.column(column, width=widths[column], anchor="center")
        self.parameter_tree.tag_configure("oddrow", background=self.colors["panel_soft"])
        self.parameter_tree.tag_configure("evenrow", background="#ffffff")
        self.parameter_tree.grid(row=1, column=0, sticky="nsew", pady=(10, 0))
        scroll = ttk.Scrollbar(table_card, orient="vertical", command=self.parameter_tree.yview)
        scroll.grid(row=1, column=1, sticky="ns", pady=(10, 0))
        self.parameter_tree.configure(yscrollcommand=scroll.set)
        self._refresh_parameter_tree()

    def _build_safety_page(self) -> None:
        page = ttk.Frame(self.notebook, padding=14)
        self.safety_page = page
        self.notebook.add(page, text="安全")
        page.columnconfigure(0, weight=1)
        page.rowconfigure(0, weight=2)
        page.rowconfigure(1, weight=1)

        safety_card = ttk.Frame(page, style="Card.TFrame", padding=12)
        safety_card.grid(row=0, column=0, sticky="nsew", pady=(0, 14))
        safety_card.rowconfigure(1, weight=1)
        safety_card.columnconfigure(0, weight=1)
        ttk.Label(safety_card, text="安全规则与运行限制", style="CardTitle.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(safety_card, textvariable=self.safety_status, style="Muted.TLabel").grid(
            row=0,
            column=0,
            sticky="e",
        )
        self.safety_tree = ttk.Treeview(
            safety_card,
            columns=("value", "status"),
            show="tree headings",
            height=12,
        )
        self.safety_tree.heading("#0", text="规则")
        self.safety_tree.heading("value", text="YAML 值")
        self.safety_tree.heading("status", text="运行状态")
        self.safety_tree.column("#0", width=300)
        self.safety_tree.column("value", width=520)
        self.safety_tree.column("status", width=120, anchor="center")
        self.safety_tree.tag_configure("oddrow", background=self.colors["panel_soft"])
        self.safety_tree.tag_configure("evenrow", background="#ffffff")
        self.safety_tree.tag_configure("triggered", background="#fee2e2", foreground=self.colors["danger"])
        self.safety_tree.grid(row=1, column=0, sticky="nsew", pady=(10, 0))
        safety_scroll = ttk.Scrollbar(safety_card, orient="vertical", command=self.safety_tree.yview)
        safety_scroll.grid(row=1, column=1, sticky="ns", pady=(10, 0))
        self.safety_tree.configure(yscrollcommand=safety_scroll.set)

        preview_card = ttk.Frame(page, style="Card.TFrame", padding=12)
        preview_card.grid(row=1, column=0, sticky="nsew")
        preview_card.rowconfigure(1, weight=1)
        preview_card.columnconfigure(0, weight=1)
        ttk.Label(preview_card, text="YAML 命令预览", style="CardTitle.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(preview_card, textvariable=self.command_preview_status, style="Muted.TLabel").grid(
            row=0,
            column=0,
            sticky="e",
        )
        self.command_preview_tree = ttk.Treeview(
            preview_card,
            columns=("phase", "source", "command", "detail"),
            show="headings",
            height=4,
        )
        self.command_preview_tree.heading("phase", text="阶段")
        self.command_preview_tree.heading("source", text="来源")
        self.command_preview_tree.heading("command", text="命令")
        self.command_preview_tree.heading("detail", text="状态")
        self.command_preview_tree.column("phase", width=150, anchor="center")
        self.command_preview_tree.column("source", width=180, anchor="center")
        self.command_preview_tree.column("command", width=300)
        self.command_preview_tree.column("detail", width=360)
        self.command_preview_tree.tag_configure("preview", background="#eff6ff")
        self.command_preview_tree.tag_configure("sent", background=self.colors["panel_soft"])
        self.command_preview_tree.grid(row=1, column=0, sticky="nsew", pady=(10, 0))
        self._populate_safety_tree(None)
        self._refresh_command_preview_tree()

    def _build_plan_page(self) -> None:
        page = ttk.Frame(self.notebook, padding=14)
        self.notebook.add(page, text="计划")
        page.columnconfigure(0, weight=1)
        page.rowconfigure(0, weight=1)
        page.rowconfigure(1, weight=0)

        plan_card = ttk.Frame(page, style="Card.TFrame", padding=12)
        plan_card.grid(row=0, column=0, sticky="nsew", pady=(0, 14))
        plan_card.rowconfigure(1, weight=1)
        plan_card.columnconfigure(0, weight=1)
        ttk.Label(plan_card, text="YAML 计划摘要", style="CardTitle.TLabel").grid(row=0, column=0, sticky="w")
        self.plan_tree = ttk.Treeview(plan_card, columns=("value",), show="tree headings", height=16)
        self.plan_tree.heading("#0", text="项目")
        self.plan_tree.heading("value", text="值")
        self.plan_tree.column("#0", width=260)
        self.plan_tree.column("value", width=720)
        self.plan_tree.tag_configure("oddrow", background=self.colors["panel_soft"])
        self.plan_tree.tag_configure("evenrow", background="#ffffff")
        self.plan_tree.grid(row=1, column=0, sticky="nsew", pady=(10, 0))
        plan_scroll = ttk.Scrollbar(plan_card, orient="vertical", command=self.plan_tree.yview)
        plan_scroll.grid(row=1, column=1, sticky="ns", pady=(10, 0))
        self.plan_tree.configure(yscrollcommand=plan_scroll.set)

        validation_card = ttk.Frame(page, style="Card.TFrame", padding=12)
        validation_card.grid(row=1, column=0, sticky="ew")
        validation_card.columnconfigure(1, weight=1)
        ttk.Label(validation_card, text="校验状态", style="CardTitle.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(validation_card, textvariable=self.plan_status, style="SmallMetric.TLabel").grid(row=0, column=1, sticky="w", padx=(14, 0))

    def _build_monitor_page(self) -> None:
        page = ttk.Frame(self.notebook, padding=14)
        self.notebook.add(page, text="监视")
        page.rowconfigure(0, weight=2)
        page.rowconfigure(1, weight=1)
        page.columnconfigure(0, weight=1)

        right = ttk.Frame(page)
        right.grid(row=0, column=0, rowspan=2, sticky="nsew")
        right.rowconfigure(0, weight=2)
        right.rowconfigure(1, weight=1)
        right.columnconfigure(0, weight=1)

        log_card = ttk.Frame(right, style="Card.TFrame", padding=12)
        log_card.grid(row=0, column=0, sticky="nsew", pady=(0, 14))
        log_card.rowconfigure(1, weight=1)
        log_card.columnconfigure(0, weight=1)
        ttk.Label(log_card, text="实时串口上下行", style="CardTitle.TLabel").grid(row=0, column=0, sticky="w")
        self.log = tk.Text(
            log_card,
            wrap="none",
            height=22,
            bg=self.colors["log_bg"],
            fg=self.colors["log_fg"],
            insertbackground="#ffffff",
            font=(self.mono_font, 10),
            relief="flat",
            bd=0,
            padx=12,
            pady=10,
            spacing1=2,
            spacing3=2,
            selectbackground="#334155",
            selectforeground="#ffffff",
            highlightthickness=1,
            highlightbackground="#243044",
            highlightcolor="#3b82f6",
        )
        self.log.grid(row=1, column=0, sticky="nsew", pady=(10, 0))
        log_scroll = ttk.Scrollbar(log_card, orient="vertical", command=self.log.yview)
        log_scroll.grid(row=1, column=1, sticky="ns", pady=(10, 0))
        self.log.configure(yscrollcommand=log_scroll.set)
        self._configure_log_tags()

        data_card = ttk.Frame(right, style="Card.TFrame", padding=12)
        data_card.grid(row=1, column=0, sticky="nsew")
        data_card.rowconfigure(1, weight=1)
        data_card.columnconfigure(0, weight=1)
        ttk.Label(data_card, text="最新解析 DAT", style="CardTitle.TLabel").grid(row=0, column=0, sticky="w")
        self.dat_tree = ttk.Treeview(data_card, columns=("value",), show="tree headings", height=8)
        self.dat_tree.heading("#0", text="字段")
        self.dat_tree.heading("value", text="值")
        self.dat_tree.column("#0", width=210)
        self.dat_tree.column("value", width=260)
        self.dat_tree.tag_configure("oddrow", background=self.colors["panel_soft"])
        self.dat_tree.tag_configure("evenrow", background="#ffffff")
        self.dat_tree.grid(row=1, column=0, sticky="nsew", pady=(10, 0))

    def _build_tuning_page(self) -> None:
        page = ttk.Frame(self.notebook, padding=14)
        self.notebook.add(page, text="调参")
        page.columnconfigure(0, weight=1)
        page.rowconfigure(1, weight=1)

        metrics = ttk.Frame(page)
        metrics.grid(row=0, column=0, sticky="ew", pady=(0, 14))
        for column in range(4):
            metrics.columnconfigure(column, weight=1)
        self._small_metric_card(metrics, "当前轮次", self.current_round, 0, 0)
        self._small_metric_card(metrics, "当前评分", self.score, 0, 1)
        self._small_metric_card(metrics, "当前决策", self.decision, 0, 2)
        self._small_metric_card(metrics, "停止原因", self.stop_reason, 0, 3)
        self._small_metric_card(metrics, "已接受", self.accepted_count, 1, 0)
        self._small_metric_card(metrics, "保持", self.held_count, 1, 1)
        self._small_metric_card(metrics, "回滚", self.rollback_count, 1, 2)
        self._small_metric_card(metrics, "失败/错误", self.failed_count, 1, 3)

        tuning_card = ttk.Frame(page, style="Card.TFrame", padding=12)
        tuning_card.grid(row=1, column=0, sticky="nsew")
        tuning_card.rowconfigure(1, weight=1)
        tuning_card.columnconfigure(0, weight=1)
        ttk.Label(tuning_card, text="执行摘要", style="CardTitle.TLabel").grid(row=0, column=0, sticky="w")
        self.tuning_tree = ttk.Treeview(tuning_card, columns=("value",), show="tree headings", height=12)
        self.tuning_tree.heading("#0", text="项目")
        self.tuning_tree.heading("value", text="值")
        self.tuning_tree.column("#0", width=220)
        self.tuning_tree.column("value", width=760)
        self.tuning_tree.tag_configure("oddrow", background=self.colors["panel_soft"])
        self.tuning_tree.tag_configure("evenrow", background="#ffffff")
        self.tuning_tree.grid(row=1, column=0, sticky="nsew", pady=(10, 0))

    def _build_replay_page(self) -> None:
        page = ttk.Frame(self.notebook, padding=14)
        self.notebook.add(page, text="回放")
        page.columnconfigure(0, weight=1)
        page.rowconfigure(1, weight=1)
        page.rowconfigure(2, weight=1)
        page.rowconfigure(3, weight=0)

        controls = ttk.Frame(page, style="Card.TFrame", padding=12)
        controls.grid(row=0, column=0, sticky="ew", pady=(0, 14))
        controls.columnconfigure(1, weight=1)
        ttk.Label(controls, text="历史会话", style="CardTitle.TLabel").grid(row=0, column=0, sticky="w", padx=(0, 10))
        ttk.Entry(controls, textvariable=self.replay_path).grid(row=0, column=1, sticky="ew", padx=(0, 8))
        ttk.Button(controls, text="选择记录", command=self.browse_replay_source).grid(row=0, column=2, padx=(0, 8))
        ttk.Button(controls, text="加载回放", command=self.load_replay_source).grid(row=0, column=3)
        ttk.Label(controls, textvariable=self.replay_status, style="Muted.TLabel").grid(
            row=1,
            column=0,
            columnspan=4,
            sticky="w",
            pady=(8, 0),
        )

        summary_card = ttk.Frame(page, style="Card.TFrame", padding=12)
        summary_card.grid(row=1, column=0, sticky="nsew", pady=(0, 14))
        summary_card.rowconfigure(1, weight=1)
        summary_card.columnconfigure(0, weight=1)
        ttk.Label(summary_card, text="回放摘要", style="CardTitle.TLabel").grid(row=0, column=0, sticky="w")
        self.replay_summary_tree = ttk.Treeview(summary_card, columns=("value",), show="tree headings", height=8)
        self.replay_summary_tree.heading("#0", text="项目")
        self.replay_summary_tree.heading("value", text="值")
        self.replay_summary_tree.column("#0", width=220)
        self.replay_summary_tree.column("value", width=760)
        self.replay_summary_tree.tag_configure("oddrow", background=self.colors["panel_soft"])
        self.replay_summary_tree.tag_configure("evenrow", background="#ffffff")
        self.replay_summary_tree.grid(row=1, column=0, sticky="nsew", pady=(10, 0))

        diff_card = ttk.Frame(page, style="Card.TFrame", padding=12)
        diff_card.grid(row=2, column=0, sticky="nsew", pady=(0, 14))
        diff_card.rowconfigure(1, weight=1)
        diff_card.columnconfigure(0, weight=1)
        ttk.Label(diff_card, text="参数差异", style="CardTitle.TLabel").grid(row=0, column=0, sticky="w")
        self.replay_diff_tree = ttk.Treeview(
            diff_card,
            columns=("baseline", "final", "delta"),
            show="tree headings",
            height=8,
        )
        self.replay_diff_tree.heading("#0", text="参数")
        self.replay_diff_tree.heading("baseline", text="基线")
        self.replay_diff_tree.heading("final", text="最终")
        self.replay_diff_tree.heading("delta", text="变化")
        self.replay_diff_tree.column("#0", width=180)
        self.replay_diff_tree.column("baseline", width=200, anchor="center")
        self.replay_diff_tree.column("final", width=200, anchor="center")
        self.replay_diff_tree.column("delta", width=200, anchor="center")
        self.replay_diff_tree.tag_configure("oddrow", background=self.colors["panel_soft"])
        self.replay_diff_tree.tag_configure("evenrow", background="#ffffff")
        self.replay_diff_tree.grid(row=1, column=0, sticky="nsew", pady=(10, 0))

        error_card = ttk.Frame(page, style="Card.TFrame", padding=12)
        error_card.grid(row=3, column=0, sticky="ew")
        error_card.columnconfigure(0, weight=1)
        ttk.Label(error_card, text="加载问题", style="CardTitle.TLabel").grid(row=0, column=0, sticky="w")
        self.replay_error_tree = ttk.Treeview(error_card, columns=("detail",), show="tree headings", height=4)
        self.replay_error_tree.heading("#0", text="状态")
        self.replay_error_tree.heading("detail", text="内容")
        self.replay_error_tree.column("#0", width=160)
        self.replay_error_tree.column("detail", width=800)
        self.replay_error_tree.tag_configure("recoverable", background="#fef3c7", foreground="#92400e")
        self.replay_error_tree.tag_configure("ok", background=self.colors["panel_soft"])
        self.replay_error_tree.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        self._refresh_replay_view(None)

    def _build_history_page(self) -> None:
        page = ttk.Frame(self.notebook, padding=14)
        self.notebook.add(page, text="历史")
        page.columnconfigure(0, weight=1)
        page.rowconfigure(0, weight=1)

        history_card = ttk.Frame(page, style="Card.TFrame", padding=12)
        history_card.grid(row=0, column=0, sticky="nsew")
        history_card.rowconfigure(1, weight=1)
        history_card.columnconfigure(0, weight=1)
        ttk.Label(history_card, text="关键事件", style="CardTitle.TLabel").grid(row=0, column=0, sticky="w")
        self.history_tree = ttk.Treeview(history_card, columns=("time", "kind", "detail"), show="headings", height=18)
        self.history_tree.heading("time", text="时间")
        self.history_tree.heading("kind", text="类型")
        self.history_tree.heading("detail", text="内容")
        self.history_tree.column("time", width=90, anchor="center")
        self.history_tree.column("kind", width=110, anchor="center")
        self.history_tree.column("detail", width=840)
        self.history_tree.tag_configure("oddrow", background=self.colors["panel_soft"])
        self.history_tree.tag_configure("evenrow", background="#ffffff")
        self.history_tree.grid(row=1, column=0, sticky="nsew", pady=(10, 0))
        history_scroll = ttk.Scrollbar(history_card, orient="vertical", command=self.history_tree.yview)
        history_scroll.grid(row=1, column=1, sticky="ns", pady=(10, 0))
        self.history_tree.configure(yscrollcommand=history_scroll.set)

    def _metric_card(self, parent: ttk.Frame, title: str, variable: tk.StringVar, row: int) -> None:
        card = ttk.Frame(parent, style="Card.TFrame", padding=12)
        card.grid(row=row, column=0, sticky="ew", pady=(0, 10))
        ttk.Label(card, text=title, style="Muted.TLabel").pack(anchor="w")
        ttk.Label(card, textvariable=variable, style="Metric.TLabel").pack(anchor="w", pady=(5, 0))

    def _small_metric_card(self, parent: ttk.Frame, title: str, variable: tk.StringVar, row: int, column: int) -> None:
        card = ttk.Frame(parent, style="Card.TFrame", padding=12)
        card.grid(row=row, column=column, sticky="ew", padx=(0 if column == 0 else 10, 0), pady=(0, 10))
        ttk.Label(card, text=title, style="Muted.TLabel").pack(anchor="w")
        ttk.Label(card, textvariable=variable, style="SmallMetric.TLabel").pack(anchor="w", pady=(5, 0))

    def _configure_log_tags(self) -> None:
        mono_bold = (self.mono_font, 10, "bold")
        self.log.tag_configure("TX", foreground="#38bdf8", font=mono_bold)
        self.log.tag_configure("RX", foreground="#4ade80")
        self.log.tag_configure("DAT", foreground="#facc15")
        self.log.tag_configure("ERR", foreground="#fb7185", font=mono_bold)
        self.log.tag_configure("ROUND", foreground="#c4b5fd", font=mono_bold)
        self.log.tag_configure("INFO", foreground=self.colors["log_fg"])

    def browse_plan(self) -> None:
        path = filedialog.askopenfilename(filetypes=[("YAML 文件", "*.yaml *.yml"), ("所有文件", "*.*")])
        if path:
            self.plan_path.set(path)
            self.validate_current_plan()

    def validate_current_plan(self) -> bool:
        if self.mode.get() == "demo" and not self.plan_path.get().strip():
            self.plan = None
            self.validated_plan_path = None
            self.status.set("演示就绪")
            self.plan_status.set("演示模式：未加载 YAML")
            self._load_demo_params()
            self._add_history_event("校验", "演示模式无需 YAML")
            return True
        path = Path(self.plan_path.get().strip())
        if not path.exists():
            self.status.set("计划缺失")
            self.plan_status.set("计划缺失")
            self.plan = None
            self.validated_plan_path = None
            self._populate_plan_tree(None)
            self._populate_connection_tree(None)
            self.safety_triggered_rules = set()
            self._populate_safety_tree(None)
            self._reset_command_preview()
            self._clear_parameter_metadata("计划缺失")
            self.connection_state.set("未连接")
            self.connection_detail.set("请先校验 YAML 计划")
            messagebox.showerror("计划缺失", "请先选择有效的 mcu_tuning_plan.yaml。")
            return False
        try:
            plan = load_plan(path)
            validator = validate_plan(plan)
        except Exception as exc:  # noqa: BLE001 - show practical UI error
            self.status.set("计划无效")
            self.plan_status.set("校验失败")
            self.plan = None
            self.validated_plan_path = None
            self._populate_plan_tree(None)
            self._populate_connection_tree(None)
            self.safety_triggered_rules = set()
            self._populate_safety_tree(None)
            self._reset_command_preview()
            self._clear_parameter_metadata("校验失败")
            self.connection_state.set("未连接")
            self.connection_detail.set("YAML 校验失败")
            messagebox.showerror("校验失败", str(exc))
            return False
        if validator.errors:
            self.status.set("计划无效")
            self.plan_status.set(f"校验失败：{len(validator.errors)} 项错误")
            self.plan = None
            self.validated_plan_path = None
            self._populate_plan_tree(plan)
            self._populate_connection_tree(plan)
            self.safety_triggered_rules = set()
            self._populate_safety_tree(None)
            self._reset_command_preview()
            self._clear_parameter_metadata("校验失败")
            self.connection_state.set("未连接")
            self.connection_detail.set("YAML 校验失败")
            self._add_history_event("校验", f"失败：{'; '.join(validator.errors[:3])}")
            messagebox.showerror("校验失败", "\n".join(validator.errors[:12]))
            return False
        self.plan = plan
        self.validated_plan_path = path.resolve()
        self._load_plan_summary(plan)
        self.status.set("计划有效")
        self.plan_status.set("计划有效")
        self._add_history_event("校验", f"通过：{path.name}")
        return True

    def _load_plan_summary(self, plan: dict[str, Any]) -> None:
        transport = plan.get("transport", {})
        self.port.set(str(transport.get("port", "-")))
        self.baudrate.set(str(transport.get("baudrate", "-")))
        if self.connection_transport is None:
            self.connection_state.set("计划已校验，未连接")
            self.connection_detail.set(f"{self.port.get()} @ {self.baudrate.get()} 已从 YAML 加载")
        self._set_parameter_metadata_from_plan(plan, reset_selection=True)
        self._refresh_overview_parameter_tree()
        self._refresh_parameter_tree()
        self._populate_plan_tree(plan)
        self._populate_connection_tree(plan)
        self.safety_triggered_rules = set()
        self._populate_safety_tree(plan)
        self._reset_command_preview()
        self._refresh_tuning_tree()

    def _load_demo_params(self) -> None:
        self.port.set("DEMO")
        self.baudrate.set("115200")
        self.connection_state.set("演示模式")
        self.connection_detail.set("演示模式未打开真实串口")
        demo_plan = self._demo_plan()
        self._set_parameter_metadata_from_plan(demo_plan, reset_selection=True)
        self._refresh_overview_parameter_tree()
        self._refresh_parameter_tree()
        self._populate_demo_plan_tree()
        self.safety_triggered_rules = set()
        self._populate_safety_tree(demo_plan)
        self._reset_command_preview()
        self._refresh_tuning_tree()

    def _populate_demo_plan_tree(self) -> None:
        self._populate_plan_tree(self._demo_plan())
        self._populate_connection_tree(self._demo_plan())

    def _demo_plan(self) -> dict[str, Any]:
        return {
            "schema_version": "demo",
            "transport": {"port": "DEMO", "baudrate": 115200, "line_ending": "\\r\\n"},
            "commands": {"status": "STATUS", "set": "SET {key} {value}", "telemetry_on": "START", "stop": "STOP"},
            "parameters": [
                {"key": "kp_x", "current": 350, "min": 100, "max": 600, "initial_step": 20, "role": "primary", "group": "x_axis"},
                {"key": "kd_x", "current": 30, "min": 0, "max": 80, "initial_step": 5, "role": "primary", "group": "x_axis"},
                {"key": "kp_y", "current": 320, "min": 100, "max": 600, "initial_step": 20, "role": "primary", "group": "y_axis"},
                {"key": "kd_y", "current": 40, "min": 0, "max": 80, "initial_step": 5, "role": "primary", "group": "y_axis"},
            ],
            "telemetry": {"format": "CSV + DAT JSON demo", "sample_period_ms": 50},
            "step_policy": {"max_rounds": 2, "parameter_order": ["kp_x", "kd_x", "kp_y", "kd_y"]},
        }

    def _display_value(self, value: Any) -> str:
        if value is None:
            return "-"
        if isinstance(value, str):
            if "\r" in value or "\n" in value:
                return repr(value)
            return value
        if isinstance(value, (list, tuple)):
            return ", ".join(str(item) for item in value) if value else "-"
        if isinstance(value, dict):
            return json.dumps(value, ensure_ascii=False)
        return str(value)

    def _ordered_plan_parameters(self, plan: dict[str, Any]) -> list[dict[str, Any]]:
        by_key = {
            str(param.get("key")): dict(param)
            for param in plan.get("parameters", [])
            if isinstance(param, dict) and param.get("key")
        }
        ordered: list[dict[str, Any]] = []
        for key in plan.get("step_policy", {}).get("parameter_order", []):
            key_text = str(key)
            if key_text in by_key and by_key[key_text] not in ordered:
                ordered.append(by_key[key_text])
        for param in by_key.values():
            if param not in ordered:
                ordered.append(param)
        return ordered

    def _set_parameter_metadata_from_plan(self, plan: dict[str, Any], *, reset_selection: bool) -> None:
        ordered_params = self._ordered_plan_parameters(plan)
        self.parameter_metadata_by_key = {str(param["key"]): dict(param) for param in ordered_params}
        self.parameter_current_values = {
            key: param.get("current", "-")
            for key, param in self.parameter_metadata_by_key.items()
        }
        self.parameter_baseline_values = {}
        self.parameter_last_stable_values = dict(self.parameter_current_values)
        self.parameter_trial_values = {}
        self.parameter_skipped_keys = set()
        if reset_selection:
            self.parameter_active_keys = None
            self.parameter_keys_text.set("")
            self.parameter_selection_status.set(f"参数就绪：{len(self.parameter_metadata_by_key)} 个参数")
        self._refresh_parameter_selection_options()

    def _clear_parameter_metadata(self, status: str = "未加载参数") -> None:
        self.parameter_metadata_by_key = {}
        self.parameter_current_values = {}
        self.parameter_baseline_values = {}
        self.parameter_last_stable_values = {}
        self.parameter_trial_values = {}
        self.parameter_active_keys = None
        self.parameter_skipped_keys = set()
        self.parameter_keys_text.set("")
        self.parameter_selection_status.set(status)
        self._refresh_parameter_selection_options()
        self._refresh_overview_parameter_tree()
        self._refresh_parameter_tree()

    def _refresh_parameter_selection_options(self) -> None:
        axes = sorted(
            {
                axis
                for param in self.parameter_metadata_by_key.values()
                for axis in [self._parameter_axis(param)]
                if axis
            }
        )
        groups = sorted(
            {
                str(param.get("group"))
                for param in self.parameter_metadata_by_key.values()
                if param.get("group") not in {None, ""}
            }
        )
        if hasattr(self, "parameter_axis_combo"):
            self.parameter_axis_combo.configure(values=axes)
        if hasattr(self, "parameter_group_combo"):
            self.parameter_group_combo.configure(values=groups)
        self.parameter_axis.set(axes[0] if axes else "")
        self.parameter_group.set(groups[0] if groups else "")

    def _parameter_axis(self, param: dict[str, Any]) -> str:
        group = str(param.get("group", "")).lower()
        key = str(param.get("key", "")).lower()
        for source in (group, key):
            parts = [part for part in re.split(r"[^a-z0-9]+", source) if part]
            for axis in ("x", "y", "z", "roll", "pitch", "yaw"):
                if axis in parts or source.startswith(f"{axis}_") or source.endswith(f"_{axis}") or source == axis:
                    return axis
        return ""

    def _declared_parameter_keys(self) -> list[str]:
        return list(self.parameter_metadata_by_key)

    def _default_active_parameter_keys(self) -> list[str]:
        if self.plan is None:
            return self._declared_parameter_keys()
        declared = set(self.parameter_metadata_by_key)
        ordered = [
            str(key)
            for key in self.plan.get("step_policy", {}).get("parameter_order", [])
            if str(key) in declared
        ]
        return ordered or self._declared_parameter_keys()

    def _active_parameter_keys_for_gui(self) -> list[str]:
        if self.parameter_active_keys is not None:
            return list(self.parameter_active_keys)
        return self._default_active_parameter_keys()

    def _refresh_overview_parameter_tree(self) -> None:
        if not hasattr(self, "param_tree"):
            return
        self.param_tree.delete(*self.param_tree.get_children())
        for index, (key, param) in enumerate(self.parameter_metadata_by_key.items()):
            current = self.parameter_current_values.get(key, param.get("current", "-"))
            bounds = f"{param.get('min', '-')}..{param.get('max', '-')}"
            role = param.get("role", "-")
            tag = "evenrow" if index % 2 == 0 else "oddrow"
            self.param_tree.insert("", "end", iid=key, values=(key, current, bounds, role), tags=(tag,))

    def _parameter_participation_label(self, key: str) -> str:
        if key in self.parameter_skipped_keys:
            return "已跳过"
        if self.session is not None and str(getattr(self.session, "current_parameter_key", "")) == key:
            return "当前"
        return "参与" if key in set(self._active_parameter_keys_for_gui()) else "未参与"

    def _refresh_parameter_tree(self) -> None:
        if not hasattr(self, "parameter_tree"):
            return
        self.parameter_tree.delete(*self.parameter_tree.get_children())
        for index, (key, param) in enumerate(self.parameter_metadata_by_key.items()):
            tag = "evenrow" if index % 2 == 0 else "oddrow"
            bounds = f"{param.get('min', '-')}..{param.get('max', '-')}"
            step_parts = []
            if param.get("initial_step") is not None:
                step_parts.append(f"initial={param.get('initial_step')}")
            if param.get("max_delta_per_round") is not None:
                step_parts.append(f"max_delta={param.get('max_delta_per_round')}")
            step = ", ".join(step_parts) if step_parts else "-"
            self.parameter_tree.insert(
                "",
                "end",
                iid=key,
                values=(
                    key,
                    self._display_value(self.parameter_current_values.get(key, param.get("current"))),
                    self._display_value(self.parameter_baseline_values.get(key)),
                    self._display_value(self.parameter_last_stable_values.get(key)),
                    self._display_value(self.parameter_trial_values.get(key)),
                    bounds,
                    step,
                    self._parameter_participation_label(key),
                ),
                tags=(tag,),
            )

    def _safety_row_tags(self, row_id: str, index: int) -> tuple[str, ...]:
        row_tag = "evenrow" if index % 2 == 0 else "oddrow"
        triggered = row_id in self.safety_triggered_rules or any(
            trigger.startswith(f"{row_id}.") for trigger in self.safety_triggered_rules
        )
        return (row_tag, "triggered") if triggered else (row_tag,)

    def _safety_row_status(self, row_id: str) -> str:
        triggered = row_id in self.safety_triggered_rules or any(
            trigger.startswith(f"{row_id}.") for trigger in self.safety_triggered_rules
        )
        return "触发" if triggered else "正常"

    def _populate_safety_tree(self, plan: dict[str, Any] | None) -> None:
        if not hasattr(self, "safety_tree"):
            return
        self.safety_tree.delete(*self.safety_tree.get_children())
        row_index = 0

        def insert(parent: str, row_id: str, label: str, value: Any = "", open_item: bool = False) -> str:
            nonlocal row_index
            tags = self._safety_row_tags(row_id, row_index)
            row_index += 1
            return str(
                self.safety_tree.insert(
                    parent,
                    "end",
                    iid=row_id,
                    text=label,
                    values=(self._display_value(value), self._safety_row_status(row_id)),
                    tags=tags,
                    open=open_item,
                )
            )

        if plan is None:
            insert("", "safety.unloaded", "未加载安全规则", "请先校验 YAML 计划", True)
            self.safety_status.set("未加载安全规则")
            return

        limits_id = insert("", "limits", "运行限制", "", True)
        transport = plan.get("transport", {})
        telemetry = plan.get("telemetry", {})
        scoring = plan.get("scoring", {})
        step_policy = plan.get("step_policy", {})
        for row_id, label, value in (
            ("transport.read_timeout_ms", "transport.read_timeout_ms", transport.get("read_timeout_ms")),
            ("transport.write_timeout_ms", "transport.write_timeout_ms", transport.get("write_timeout_ms")),
            ("telemetry.sample_period_ms", "telemetry.sample_period_ms", telemetry.get("sample_period_ms")),
            ("scoring.baseline_window_ms", "scoring.baseline_window_ms", scoring.get("baseline_window_ms")),
            ("scoring.trial_window_ms", "scoring.trial_window_ms", scoring.get("trial_window_ms")),
            ("step_policy.max_rounds", "step_policy.max_rounds", step_policy.get("max_rounds")),
            ("step_policy.cooldown_ms", "step_policy.cooldown_ms", step_policy.get("cooldown_ms")),
        ):
            insert(limits_id, row_id, label, value)

        for param in self._ordered_plan_parameters(plan):
            key = str(param.get("key", "-"))
            insert(limits_id, f"parameters.{key}.range", f"{key} range", f"{param.get('min', '-')}..{param.get('max', '-')}")
            insert(limits_id, f"parameters.{key}.max_delta_per_round", f"{key} max_delta", param.get("max_delta_per_round"))

        stop_id = insert("", "stop_conditions", "停止条件", "", True)
        stop_conditions = plan.get("stop_conditions", {})
        if isinstance(stop_conditions, dict) and stop_conditions:
            for key, value in stop_conditions.items():
                insert(stop_id, f"stop_conditions.{key}", f"stop_conditions.{key}", value)
        else:
            insert(stop_id, "stop_conditions.unset", "stop_conditions", "未定义")

        hard_id = insert("", "hard_failures", "Hard failure 规则", "", True)
        hard_failures = scoring.get("hard_failures", [])
        if isinstance(hard_failures, list) and hard_failures:
            for index, rule in enumerate(hard_failures):
                insert(hard_id, f"scoring.hard_failures.{index}", f"hard_failure[{index}]", rule)
        else:
            insert(hard_id, "scoring.hard_failures.unset", "hard_failures", "未定义")

        telemetry_id = insert("", "telemetry_safety", "安全遥测字段", "", True)
        safety_fields = []
        for field in telemetry.get("fields", []):
            if not isinstance(field, dict):
                continue
            name = str(field.get("name", ""))
            role = str(field.get("role", ""))
            if role == "safety" or name in {"lost", "sat"}:
                safety_fields.append(field)
        if safety_fields:
            for field in safety_fields:
                name = str(field.get("name"))
                value = f"role={field.get('role', '-')}, type={field.get('type', '-')}, unit={field.get('unit', '-')}"
                insert(telemetry_id, f"telemetry.{name}", f"telemetry.{name}", value)
        else:
            insert(telemetry_id, "telemetry_safety.unset", "安全遥测字段", "未定义")

        triggered_count = len(self.safety_triggered_rules)
        self.safety_status.set(f"规则就绪：{max(row_index - 4, 0)} 条；触发：{triggered_count} 条")

    def _refresh_command_preview_tree(self) -> None:
        if not hasattr(self, "command_preview_tree"):
            return
        self.command_preview_tree.delete(*self.command_preview_tree.get_children())
        rows = [
            ("next", "下一条 YAML 命令", self.command_preview, "preview"),
            ("last", "最近 TX", self.last_sent_command, "sent"),
        ]
        for row_id, phase, payload, tag in rows:
            self.command_preview_tree.insert(
                "",
                "end",
                iid=row_id,
                values=(
                    phase,
                    payload.get("source", "-"),
                    payload.get("command", "-"),
                    payload.get("detail", "-"),
                ),
                tags=(tag,),
            )

    def _set_command_preview(self, source: str, command: Any, detail: str) -> None:
        command_text = self._display_value(command)
        self.command_preview = {
            "source": source or "-",
            "command": command_text,
            "detail": detail,
        }
        self.command_preview_status.set(f"{source}: {command_text}" if source and command_text != "-" else detail)
        self._refresh_command_preview_tree()

    def _set_last_sent_command(self, source: str, command: Any, detail: str) -> None:
        self.last_sent_command = {
            "source": source or "-",
            "command": self._display_value(command),
            "detail": detail,
        }
        self._refresh_command_preview_tree()

    def _reset_command_preview(self) -> None:
        self.last_sent_command = {"source": "-", "command": "-", "detail": "尚未发送"}
        if self.plan is None:
            self._set_command_preview("-", "-", "未加载 YAML 计划")
            return
        self._preview_static_command("status", "计划校验后下一条会话命令")

    def _preview_static_command(self, command_key: str, detail: str) -> None:
        if self.plan is None:
            self._set_command_preview("-", "-", "未加载 YAML 计划")
            return
        command = self.plan.get("commands", {}).get(command_key)
        if command:
            self._set_command_preview(f"commands.{command_key}", command, detail)
        else:
            self._set_command_preview("-", "-", f"YAML 未定义 commands.{command_key}")

    def _format_yaml_command(self, command_key: str, key: Any = None, value: Any = None) -> str | None:
        if self.plan is None:
            return None
        template = self.plan.get("commands", {}).get(command_key)
        if not template:
            return None
        template_text = str(template)
        if "{key" in template_text or "{value" in template_text:
            if key is None or value is None:
                return None
            try:
                return format_template(template_text, str(key), value)
            except (ValueError, TypeError):
                try:
                    return template_text.format(key=str(key), value=value)
                except Exception:
                    return None
        return template_text

    def _format_rollback_preview_command(self, key: Any, value: Any) -> str | None:
        if self.plan is None:
            return None
        template = self.plan.get("rollback", {}).get("command_template")
        if not template:
            return None
        try:
            return format_template(str(template), str(key), value)
        except (ValueError, TypeError):
            try:
                return str(template).format(key=str(key), value=value)
            except Exception:
                return None

    def _preview_set_command(self, key: Any, value: Any, detail: str) -> None:
        command = self._format_yaml_command("set", key, value)
        if command is None:
            self._set_command_preview("-", "-", "无法从 YAML commands.set 生成预览")
            return
        self._set_command_preview("commands.set", command, detail)

    def _source_for_sent_command(self, command: str) -> str:
        if self.plan is None:
            return "-"
        commands = self.plan.get("commands", {})
        for key in ("status", "telemetry_on", "telemetry_off", "stop"):
            if command == str(commands.get(key, "")):
                return f"commands.{key}"
        set_template = str(commands.get("set", ""))
        set_prefix = set_template.split("{key", 1)[0] if "{key" in set_template else set_template
        if set_prefix and command.startswith(set_prefix):
            return "commands.set"
        rollback_template = str(self.plan.get("rollback", {}).get("command_template", ""))
        rollback_prefix = rollback_template.split("{key", 1)[0] if "{key" in rollback_template else rollback_template
        if rollback_prefix and command.startswith(rollback_prefix):
            return "rollback.command_template"
        return "YAML-derived"

    def _preview_after_sent_command(self, source: str) -> None:
        if source == "commands.status":
            if self.plan and self.plan.get("commands", {}).get("telemetry_on"):
                self._preview_static_command("telemetry_on", "STATUS 后的下一条 YAML 命令")
            return
        if source == "commands.telemetry_on":
            self._set_command_preview("-", "-", "采集基线遥测，不发送新命令")
            return
        if source == "commands.set":
            if self.plan and self.plan.get("commands", {}).get("readback_required"):
                self._preview_static_command("status", "SET 后的 YAML readback 命令")
            else:
                self._set_command_preview("-", "-", "等待试验遥测，不发送新命令")
            return
        if source == "commands.telemetry_off":
            self._preview_static_command("stop", "telemetry_off 后的 YAML stop 命令")
            return
        if source == "commands.stop":
            self._set_command_preview("-", "-", "停止命令已发送，无待发送 YAML 命令")

    def _mark_safety_triggers(self, row_ids: list[str], detail: str) -> None:
        new_ids = [row_id for row_id in row_ids if row_id and row_id not in self.safety_triggered_rules]
        if not new_ids:
            return
        self.safety_triggered_rules.update(new_ids)
        self._populate_safety_tree(self.plan)
        self._add_history_event("安全", detail)

    def _hard_failure_row_ids_for(self, failure_or_field: str) -> list[str]:
        if self.plan is None:
            return []
        target = str(failure_or_field)
        row_ids: list[str] = []
        hard_failures = self.plan.get("scoring", {}).get("hard_failures", [])
        if not isinstance(hard_failures, list):
            return row_ids
        for index, rule in enumerate(hard_failures):
            rule_text = str(rule)
            if target == rule_text or re.search(rf"\b{re.escape(target)}\b", rule_text):
                row_ids.append(f"scoring.hard_failures.{index}")
        return row_ids

    def _truthy_safety_value(self, value: Any) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return value != 0
        if isinstance(value, str):
            return value.strip().lower() not in {"", "0", "false", "none", "no"}
        return bool(value)

    def _is_plan_safety_field(self, field_name: str) -> bool:
        if self.plan is None:
            return False
        for field in self.plan.get("telemetry", {}).get("fields", []):
            if not isinstance(field, dict):
                continue
            if str(field.get("name")) == field_name and str(field.get("role", "")) == "safety":
                return True
        return False

    def _update_safety_from_sample(self, sample: dict[str, Any]) -> None:
        row_ids: list[str] = []
        triggered_fields: list[str] = []
        for key, value in sample.items():
            field = str(key)
            if field not in {"lost", "sat"} and not self._is_plan_safety_field(field):
                continue
            if not self._truthy_safety_value(value):
                continue
            triggered_fields.append(field)
            row_ids.append(f"telemetry.{field}")
            row_ids.extend(self._hard_failure_row_ids_for(field))
        if triggered_fields:
            row_ids.append("stop_conditions.unsafe_behavior")
            self._mark_safety_triggers(row_ids, f"安全遥测触发：{', '.join(triggered_fields)}")

    def _update_safety_from_structured_event(self, event: TuningEvent, line: str) -> None:
        row_ids: list[str] = []
        data = event.data
        if event.type == "dat":
            sample = data.get("sample")
            if isinstance(sample, dict):
                self._update_safety_from_sample(sample)
            return
        if event.type == "decision":
            hard_failures = data.get("hard_failures", [])
            if isinstance(hard_failures, list):
                for failure in hard_failures:
                    row_ids.extend(self._hard_failure_row_ids_for(str(failure)))
                if hard_failures:
                    row_ids.append("stop_conditions.unsafe_behavior")
                    self._mark_safety_triggers(row_ids, f"决策触发 hard failure：{', '.join(map(str, hard_failures))}")
            return
        if event.type == "error":
            category = str(data.get("category", ""))
            code = str(data.get("code") or data.get("error_code") or "")
            if category == "ok_timeout" or code == "ok_timeout":
                row_ids.append("stop_conditions.communication_failure")
            if category in {"malformed_telemetry", "hard_failure"} or code in {"malformed_telemetry", "hard_failure"}:
                row_ids.append("stop_conditions.unsafe_behavior")
            context = data.get("context", {})
            hard_failures = data.get("hard_failures")
            if hard_failures is None and isinstance(context, dict):
                hard_failures = context.get("hard_failures")
            if isinstance(hard_failures, list):
                for failure in hard_failures:
                    row_ids.extend(self._hard_failure_row_ids_for(str(failure)))
            if row_ids:
                self._mark_safety_triggers(row_ids, f"错误事件触发安全规则：{line}")

    def _update_command_preview_from_structured_event(self, event: TuningEvent) -> None:
        if event.type == "state":
            next_state = str(event.data.get("next_state", ""))
            if next_state == "connected":
                self._preview_static_command("status", "connected 后发送前预览")
            elif next_state == "baseline":
                self._set_command_preview("-", "-", "采集基线遥测，不发送新命令")
            elif next_state == "tuning":
                self._set_command_preview("-", "-", "等待下一轮参数事件")
            elif next_state == "stopping":
                if self.plan and self.plan.get("commands", {}).get("telemetry_off"):
                    self._preview_static_command("telemetry_off", "stopping 清理发送前预览")
                else:
                    self._preview_static_command("stop", "stopping 清理发送前预览")
            elif next_state in {"stopped", "error"}:
                self._set_command_preview("-", "-", f"{next_state} 状态无待发送 YAML 命令")
            return
        if event.type == "round":
            key = event.data.get("key")
            trial_value = event.data.get("trial_value")
            if key is not None and trial_value is not None:
                round_index = event.data.get("round", "-")
                self._preview_set_command(key, trial_value, f"第 {round_index} 轮发送前预览")
            return
        if event.type == "tx":
            command = str(event.data.get("command") or event.message).strip()
            source = self._source_for_sent_command(command)
            self._set_last_sent_command(source, command, "已由会话发送")
            self._preview_after_sent_command(source)
            return
        if event.type == "summary":
            self._set_command_preview("-", "-", "会话已总结，无待发送 YAML 命令")

    def _reject_parameter_selection(self, detail: str) -> bool:
        self.parameter_selection_status.set(f"选择拒绝：{detail}")
        self._add_history_event("参数", f"选择拒绝：{detail}")
        self._refresh_parameter_tree()
        return False

    def _apply_parameter_selection(self, keys: list[str], label: str) -> bool:
        if self.plan is None and not self.parameter_metadata_by_key:
            return self._reject_parameter_selection("请先校验 YAML 计划")
        if self._task_running():
            return self._reject_parameter_selection("当前任务运行中，不能修改本轮参数选择")

        normalized: list[str] = []
        for key in keys:
            key_text = str(key).strip()
            if key_text and key_text not in normalized:
                normalized.append(key_text)
        if not normalized:
            return self._reject_parameter_selection("没有匹配的参数")

        unknown = [key for key in normalized if key not in self.parameter_metadata_by_key]
        if unknown:
            return self._reject_parameter_selection(f"未知参数：{', '.join(unknown)}")

        self.parameter_active_keys = normalized
        self.parameter_keys_text.set(", ".join(normalized))
        self.parameter_selection_status.set(f"已选择 {label}：{', '.join(normalized)}")
        self._add_history_event("参数", f"本轮参与参数：{', '.join(normalized)}")
        self._refresh_parameter_tree()
        return True

    def select_all_parameters(self) -> bool:
        return self._apply_parameter_selection(self._declared_parameter_keys(), "全部")

    def apply_axis_parameter_selection(self) -> bool:
        axis = self.parameter_axis.get().strip()
        keys = [
            key
            for key, param in self.parameter_metadata_by_key.items()
            if self._parameter_axis(param) == axis
        ]
        return self._apply_parameter_selection(keys, f"轴 {axis}" if axis else "轴")

    def apply_group_parameter_selection(self) -> bool:
        group = self.parameter_group.get().strip()
        keys = [
            key
            for key, param in self.parameter_metadata_by_key.items()
            if str(param.get("group", "")) == group
        ]
        return self._apply_parameter_selection(keys, f"分组 {group}" if group else "分组")

    def apply_specific_parameter_selection(self) -> bool:
        text = self.parameter_keys_text.get()
        keys = [part for part in re.split(r"[\s,;]+", text) if part]
        return self._apply_parameter_selection(keys, "指定键")

    def _sync_parameter_state_from_session(self) -> None:
        if self.session is None:
            return
        current = getattr(self.session, "current", None)
        if isinstance(current, dict):
            self.parameter_current_values.update({str(key): value for key, value in current.items()})
        baseline = getattr(self.session, "baseline_parameters", None)
        if isinstance(baseline, dict):
            self.parameter_baseline_values.update({str(key): value for key, value in baseline.items()})
        last_stable = getattr(self.session, "last_stable", None)
        if isinstance(last_stable, dict):
            self.parameter_last_stable_values.update({str(key): value for key, value in last_stable.items()})
        active_keys = getattr(self.session, "active_parameter_keys", None)
        if isinstance(active_keys, list):
            self.parameter_active_keys = [str(key) for key in active_keys]
        skipped_keys = getattr(self.session, "skipped_parameter_keys", None)
        if isinstance(skipped_keys, (set, list, tuple)):
            self.parameter_skipped_keys = {str(key) for key in skipped_keys}

    def _populate_plan_tree(self, plan: dict[str, Any] | None) -> None:
        if not hasattr(self, "plan_tree"):
            return
        self.plan_tree.delete(*self.plan_tree.get_children())
        row_index = 0

        def insert(parent: str, label: str, value: Any = "", open_item: bool = False) -> str:
            nonlocal row_index
            tag = "evenrow" if row_index % 2 == 0 else "oddrow"
            row_index += 1
            return str(
                self.plan_tree.insert(
                    parent,
                    "end",
                    text=label,
                    values=(self._display_value(value),),
                    tags=(tag,),
                    open=open_item,
                )
            )

        if plan is None:
            insert("", "未加载计划", "请选择或生成 mcu_tuning_plan.yaml", True)
            return

        insert("", "schema_version", plan.get("schema_version", "-"))
        transport_id = insert("", "transport", "", True)
        for key in ("port", "baudrate", "line_ending", "timeout_ms", "read_timeout_ms"):
            insert(transport_id, key, plan.get("transport", {}).get(key))

        commands_id = insert("", "commands", "", True)
        commands = plan.get("commands", {})
        for key in ("status", "set", "telemetry_on", "telemetry_off", "stop", "ok_pattern", "readback_required"):
            insert(commands_id, key, commands.get(key))

        params_id = insert("", "parameters", f"{len(plan.get('parameters', []))} 个参数", True)
        for param in plan.get("parameters", []):
            label = str(param.get("key", "-"))
            value = f"current={param.get('current', '-')}, range={param.get('min', '-')}..{param.get('max', '-')}, role={param.get('role', '-')}"
            insert(params_id, label, value)

        telemetry_id = insert("", "telemetry", "", True)
        telemetry = plan.get("telemetry", {})
        for key in ("format", "sample_period_ms", "scale", "parser"):
            insert(telemetry_id, key, telemetry.get(key))
        fields = telemetry.get("fields", [])
        if fields:
            field_names = [str(field.get("name", field)) if isinstance(field, dict) else str(field) for field in fields]
            insert(telemetry_id, "fields", field_names)

        scoring_id = insert("", "scoring", "", True)
        scoring = plan.get("scoring", {})
        for key in ("formula", "baseline_window_ms", "trial_window_ms", "accept_if_improvement_gte", "rollback_if_degradation_gte"):
            insert(scoring_id, key, scoring.get(key))

        policy_id = insert("", "step_policy", "", True)
        policy = plan.get("step_policy", {})
        for key in ("max_rounds", "cooldown_ms", "parameter_order", "step_shrink"):
            insert(policy_id, key, policy.get(key))

        rollback_id = insert("", "rollback", "", True)
        for key, value in plan.get("rollback", {}).items():
            insert(rollback_id, key, value)

        stop_id = insert("", "stop_conditions", "", True)
        stop_conditions = plan.get("stop_conditions", {})
        if isinstance(stop_conditions, dict):
            for key, value in stop_conditions.items():
                insert(stop_id, key, value)
        else:
            insert(stop_id, "conditions", stop_conditions)

    def _populate_connection_tree(self, plan: dict[str, Any] | None) -> None:
        if not hasattr(self, "connection_tree"):
            return
        self.connection_tree.delete(*self.connection_tree.get_children())
        row_index = 0

        def insert(label: str, value: Any) -> None:
            nonlocal row_index
            tag = "evenrow" if row_index % 2 == 0 else "oddrow"
            row_index += 1
            self.connection_tree.insert(
                "",
                "end",
                text=label,
                values=(self._display_value(value),),
                tags=(tag,),
            )

        insert("连接状态", self.connection_state.get())
        insert("检测端口", self.detected_ports.get())
        if plan is None:
            insert("YAML transport", "未加载")
            insert("连接要求", "真实传输连接前必须先校验 YAML 计划")
            return

        transport = plan.get("transport", {})
        for key in (
            "port",
            "baudrate",
            "data_bits",
            "parity",
            "stop_bits",
            "line_ending",
            "read_timeout_ms",
            "write_timeout_ms",
        ):
            insert(key, transport.get(key))
        mode = "真实 pyserial 传输" if self.session_transport_factory is None else "注入传输"
        insert("transport_mode", mode)
        insert("连接要求", "已校验 YAML 计划")

    def _set_connection_button_enabled(self, name: str, enabled: bool) -> None:
        button = self.connection_buttons.get(name)
        if button is None:
            return
        button.configure(state="normal" if enabled else "disabled")

    def _refresh_connection_controls(self) -> None:
        if not self.connection_buttons:
            return
        running = self._task_running()
        connected = self.connection_transport is not None
        self._set_connection_button_enabled("detect_ports", not running)
        self._set_connection_button_enabled("connect", not running and not connected)
        self._set_connection_button_enabled("disconnect", connected)
        self._populate_connection_tree(self.plan)

    def detect_ports_action(self) -> list[str]:
        try:
            ports = list(list_ports.comports())
        except Exception as exc:  # noqa: BLE001 - port enumeration can fail by platform
            self.detected_ports.set("检测失败")
            self.connection_detail.set(f"端口检测失败：{exc}")
            self._add_history_event("连接", f"端口检测失败：{exc}")
            self._refresh_connection_controls()
            return []

        labels: list[str] = []
        for port_info in ports:
            device = str(getattr(port_info, "device", "") or getattr(port_info, "name", "") or port_info)
            description = str(getattr(port_info, "description", "") or "")
            labels.append(f"{device} ({description})" if description and description != device else device)
        self.detected_ports.set(", ".join(labels) if labels else "未发现串口")
        if self.connection_transport is None:
            self.connection_state.set("已检测，未连接")
        self.connection_detail.set("端口检测未建立连接，也未发送串口命令")
        self._add_history_event("连接", f"端口检测：{self.detected_ports.get()}")
        self._refresh_connection_controls()
        return labels

    def _reject_connection_action(self, detail: str) -> bool:
        self.connection_state.set("连接拒绝")
        self.connection_detail.set(detail)
        self._add_history_event("连接", f"拒绝：{detail}")
        self._refresh_connection_controls()
        return False

    def connect_transport(self) -> bool:
        if self.connection_transport is not None:
            self.connection_state.set("已连接")
            self.connection_detail.set("当前已有活动连接")
            self._refresh_connection_controls()
            return True
        if self._task_running():
            return self._reject_connection_action("当前任务运行中，不能单独连接")
        if self.plan is None or self.validated_plan_path is None:
            return self._reject_connection_action("请先校验 YAML 计划")
        selected_path = self.plan_path.get().strip()
        if selected_path and Path(selected_path).resolve() != self.validated_plan_path:
            return self._reject_connection_action("YAML 计划已变更，请重新校验")

        transport_config = self.plan.get("transport")
        if not isinstance(transport_config, dict):
            return self._reject_connection_action("YAML transport 配置缺失")

        factory = self.session_transport_factory or PySerialTransport.from_config
        port = str(transport_config.get("port", "-"))
        baudrate = str(transport_config.get("baudrate", "-"))
        try:
            self.connection_transport = factory(transport_config).open()
        except Exception as exc:  # noqa: BLE001 - keep GUI responsive on platform/driver failures
            self.connection_transport = None
            self.connection_state.set("连接失败")
            self.connection_detail.set(f"{port} @ {baudrate} 连接失败：{exc}")
            self._add_history_event("连接", f"失败：{exc}")
            self._refresh_connection_controls()
            return False

        self.connection_state.set("已连接")
        self.connection_detail.set(f"{port} @ {baudrate} 已连接")
        self.status.set("已连接")
        self._add_history_event("连接", f"已连接：{port} @ {baudrate}")
        self._refresh_connection_controls()
        return True

    def disconnect_transport(self, *, record_history: bool = True) -> bool:
        if self.connection_transport is None:
            self.connection_state.set("未连接")
            self.connection_detail.set("无活动连接")
            self._refresh_connection_controls()
            return False

        transport = self.connection_transport
        self.connection_transport = None
        try:
            transport.close()
        except Exception as exc:  # noqa: BLE001 - close failures should not leave stale GUI state
            self.connection_state.set("断开失败")
            self.connection_detail.set(f"断开连接失败：{exc}")
            if record_history:
                self._add_history_event("连接", f"断开失败：{exc}")
            self._refresh_connection_controls()
            return False

        self.connection_state.set("已断开")
        self.connection_detail.set("连接已关闭")
        if record_history:
            self._add_history_event("连接", "已断开")
        self._refresh_connection_controls()
        return True

    def _refresh_tuning_tree(self) -> None:
        if not hasattr(self, "tuning_tree"):
            return
        rows = [
            ("运行模式", {"demo": "演示", "monitor": "观测", "run": "自动调参"}.get(self.mode.get(), self.mode.get())),
            ("状态", self.status.get()),
            ("当前轮次", self.current_round.get()),
            ("当前评分", self.score.get()),
            ("当前决策", self.decision.get()),
            ("已接受", self.accepted_count.get()),
            ("保持", self.held_count.get()),
            ("回滚", self.rollback_count.get()),
            ("失败/错误", self.failed_count.get()),
            ("停止原因", self.stop_reason.get()),
            ("记录文件", self.transcript_path.get()),
        ]
        self.tuning_tree.delete(*self.tuning_tree.get_children())
        for index, (label, value) in enumerate(rows):
            tag = "evenrow" if index % 2 == 0 else "oddrow"
            self.tuning_tree.insert("", "end", text=label, values=(value,), tags=(tag,))
        self._refresh_operator_controls()
        self._refresh_connection_controls()

    def browse_replay_source(self) -> None:
        path = filedialog.askopenfilename(
            filetypes=[
                ("会话记录", "*.json *.jsonl"),
                ("所有文件", "*.*"),
            ]
        )
        if path:
            self.load_replay_file(Path(path))

    def load_replay_source(self) -> bool:
        raw_path = self.replay_path.get().strip()
        if not raw_path:
            self.replay_status.set("未选择历史会话")
            self._refresh_replay_view(None)
            return False
        return self.load_replay_file(Path(raw_path))

    def load_replay_file(self, path: Path | str) -> bool:
        replay = load_session_replay(path)
        self.replay_path.set(str(path))
        self.replay_data = replay
        self._refresh_replay_view(replay)
        if replay.load_errors:
            first_error = replay.load_errors[0]
            prefix = "可恢复加载问题" if replay.recoverable else "加载失败"
            self._add_history_event("回放", f"{prefix}: {first_error}")
        else:
            self._add_history_event("回放", f"已加载：{replay.session_id}")
        return bool(replay.final_summary) and replay.recoverable

    def _refresh_replay_view(self, replay: SessionReplayLoadResult | None) -> None:
        if not hasattr(self, "replay_summary_tree"):
            return
        self.replay_summary_tree.delete(*self.replay_summary_tree.get_children())
        self.replay_diff_tree.delete(*self.replay_diff_tree.get_children())
        self.replay_error_tree.delete(*self.replay_error_tree.get_children())

        if replay is None:
            self.replay_status.set("未加载回放")
            self.replay_error_tree.insert("", "end", text="待加载", values=("选择 JSON/JSONL 会话工件后加载",), tags=("ok",))
            return

        summary = replay.final_summary
        score_delta = _numeric_delta(summary.get("baseline_score"), summary.get("final_score"))
        rows = [
            ("session_id", replay.session_id or "-"),
            ("plan_path", replay.plan_path or "-"),
            ("jsonl_path", replay.jsonl_path or "-"),
            ("transcript_path", replay.transcript_path or "-"),
            ("baseline_score", summary.get("baseline_score", "-")),
            ("final_score", summary.get("final_score", "-")),
            ("score_delta", score_delta),
            ("accepted", summary.get("accepted", "-")),
            ("rolled_back", summary.get("rolled_back", "-")),
            ("event_counts", replay.event_counts),
        ]
        for index, (label, value) in enumerate(rows):
            tag = "evenrow" if index % 2 == 0 else "oddrow"
            self.replay_summary_tree.insert("", "end", text=label, values=(self._display_value(value),), tags=(tag,))

        for index, row in enumerate(replay.parameter_differences):
            tag = "evenrow" if index % 2 == 0 else "oddrow"
            self.replay_diff_tree.insert(
                "",
                "end",
                text=str(row.get("key", "-")),
                values=(
                    self._display_value(row.get("baseline")),
                    self._display_value(row.get("final")),
                    self._display_value(row.get("delta")),
                ),
                tags=(tag,),
            )

        if replay.load_errors:
            status = "可恢复加载问题" if replay.recoverable else "加载失败"
            for error in replay.load_errors:
                self.replay_error_tree.insert("", "end", text=status, values=(error,), tags=("recoverable",))
            self.replay_status.set(f"{status}：{len(replay.load_errors)} 项；session={replay.session_id or '-'}")
        else:
            self.replay_error_tree.insert("", "end", text="ok", values=("回放加载完成",), tags=("ok",))
            self.replay_status.set(f"已加载回放：{replay.session_id or '-'}")

    def _add_history_event(self, kind: str, detail: str) -> None:
        compact_detail = detail.strip()
        if len(compact_detail) > 180:
            compact_detail = compact_detail[:177] + "..."
        self.latest_event.set(f"{kind}: {compact_detail}" if compact_detail else kind)
        if not hasattr(self, "history_tree"):
            return
        children = self.history_tree.get_children()
        if len(children) >= 500:
            self.history_tree.delete(children[0])
        index = len(self.history_tree.get_children())
        tag = "evenrow" if index % 2 == 0 else "oddrow"
        self.history_tree.insert(
            "",
            "end",
            values=(datetime.now().strftime("%H:%M:%S"), kind, compact_detail),
            tags=(tag,),
        )
        self.history_tree.see(self.history_tree.get_children()[-1])

    def _increment_counter(self, variable: tk.StringVar) -> None:
        try:
            current = int(variable.get())
        except ValueError:
            current = 0
        variable.set(str(current + 1))

    def _update_parameter_values(self, params: dict[str, Any]) -> None:
        if not hasattr(self, "param_tree"):
            return
        for key, value in params.items():
            item_id = str(key)
            self.parameter_current_values[item_id] = value
            if not self.param_tree.exists(item_id):
                continue
            old_values = list(self.param_tree.item(item_id, "values"))
            while len(old_values) < 4:
                old_values.append("-")
            old_values[1] = str(value)
            self.param_tree.item(item_id, values=old_values)
        self._refresh_parameter_tree()

    def _task_running(self) -> bool:
        return self.proc is not None or (self.session_worker is not None and self.session_worker.is_alive())

    def _session_state(self) -> str:
        state = getattr(self.session, "state", "")
        return str(state) if state is not None else ""

    def _set_operator_button_enabled(self, name: str, enabled: bool) -> None:
        button = self.operator_buttons.get(name)
        if button is None:
            return
        button.configure(state="normal" if enabled else "disabled")

    def _refresh_operator_controls(self) -> None:
        if not self.operator_buttons:
            return
        running = self._task_running()
        session_running = self.session_worker is not None and self.session_worker.is_alive()
        state = self._session_state()
        terminal_state = state in {"stopping", "stopped", "error"}
        can_start = not running
        can_control_session = session_running and not terminal_state
        current_key = getattr(self.session, "current_parameter_key", None)
        baseline = getattr(self.session, "baseline_parameters", None)

        self._set_operator_button_enabled("start_monitor", can_start)
        self._set_operator_button_enabled("start_auto_tune", can_start)
        self._set_operator_button_enabled("pause", can_control_session and state != "paused")
        self._set_operator_button_enabled("resume", session_running and state == "paused")
        self._set_operator_button_enabled("skip_current_param", can_control_session and current_key is not None)
        self._set_operator_button_enabled("rollback_last_stable", can_control_session and baseline is not None)
        self._set_operator_button_enabled("rollback_baseline", can_control_session and baseline is not None)
        self._set_operator_button_enabled("stop", running and state not in {"stopped", "error"})
        self._set_operator_button_enabled("emergency_stop", running and state not in {"stopped", "error"})

    def start_monitor(self) -> None:
        self.mode.set("monitor")
        self.start()

    def start_auto_tune(self) -> None:
        self.mode.set("run")
        self.start()

    def start(self) -> None:
        if self._task_running():
            messagebox.showinfo("正在运行", "当前已有任务正在运行。")
            return
        if self.connection_transport is not None:
            messagebox.showinfo("连接已打开", "请先在连接页断开当前连接，再启动观测或自动调参。")
            return
        self.clear_log()
        self.stop_requested = False
        backend = select_execution_backend(self.mode.get(), self.run_backend)
        if backend == "demo":
            self._start_demo()
            return
        if not self.validate_current_plan():
            return
        if backend == "session":
            self._start_session_worker()
            return
        self._start_subprocess()

    def _start_subprocess(self) -> None:
        args = self._build_process_args()
        self._open_transcript_file()
        self.status.set("运行中")
        self.session = None
        self.session_worker = None
        self._add_history_event("启动", f"{self.mode.get()} 模式（子进程）")
        self._refresh_tuning_tree()
        self.proc = subprocess.Popen(
            args,
            cwd=str(SCRIPT_DIR),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )
        self.reader_thread = threading.Thread(target=self._read_process_output, daemon=True)
        self.reader_thread.start()

    def _start_session_worker(self) -> None:
        if self.plan is None:
            messagebox.showerror("计划缺失", "请先校验有效的 mcu_tuning_plan.yaml。")
            return
        plan_path = Path(self.plan_path.get().strip())
        log_dir = self._session_log_dir()
        self.session_worker = SessionWorker(
            plan=self.plan,
            plan_path=plan_path,
            log_dir=log_dir,
            output_queue=self.output_queue,
            transport_factory=self.session_transport_factory,
            active_parameter_keys=self.parameter_active_keys,
        )
        self.session = self.session_worker.session
        self._open_transcript_file(prefix=self.session.session_id)
        self._prepare_session_artifacts(log_dir)
        self.status.set("运行中")
        self._add_history_event("启动", "自动调参模式（TuningSession worker）")
        self._refresh_tuning_tree()
        self.session_worker.start()

    def _build_process_args(self) -> list[str]:
        plan = self.plan_path.get().strip()
        if self.mode.get() == "monitor":
            return [
                sys.executable,
                str(MONITOR_SCRIPT),
                plan,
                "--start-telemetry",
                "--stop-on-exit",
                "--duration",
                "0",
            ]
        return [sys.executable, str(RUN_SCRIPT), plan]

    def _start_demo(self) -> None:
        self._load_demo_params()
        self._open_transcript_file(prefix="desktop_demo")
        self.status.set("演示运行中")
        self.stop_reason.set("-")
        self._add_history_event("启动", "演示模式")
        self._refresh_tuning_tree()
        self.demo_index = 0
        self.after(250, self._emit_demo_line)

    def _emit_demo_line(self) -> None:
        if self.stop_requested or self.demo_index >= len(DEMO_LINES):
            self.status.set("演示完成" if not self.stop_requested else "已停止")
            self._close_transcript_file()
            return
        self._handle_line(DEMO_LINES[self.demo_index])
        self.demo_index += 1
        self.after(420, self._emit_demo_line)

    def _read_process_output(self) -> None:
        assert self.proc is not None
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            self.output_queue.put(line.rstrip("\n"))
        code = self.proc.wait()
        self.output_queue.put(f"INFO Process exited with code {code}")
        self.output_queue.put("__PROCESS_DONE__")

    def _pump_output(self) -> None:
        while True:
            try:
                item = self.output_queue.get_nowait()
            except queue.Empty:
                break
            if isinstance(item, tuple) and item:
                kind = item[0]
                if kind == SESSION_EVENT:
                    self._handle_session_event(item[1])
                elif kind == SESSION_ERROR:
                    self._handle_session_error(item[1])
                elif kind == SESSION_DONE:
                    self._handle_session_done(item[1])
                continue
            line = str(item)
            if line == "__PROCESS_DONE__":
                self.proc = None
                self.status.set("空闲")
                self._add_history_event("结束", "子进程已退出")
                self._close_transcript_file()
                self._refresh_tuning_tree()
            else:
                self._handle_line(line)
        self.after(80, self._pump_output)

    def _handle_session_event(self, event: TuningEvent) -> None:
        line = self._session_event_to_line(event)
        self._append_log_line(line, self._session_event_log_tag(event))
        self._update_command_preview_from_structured_event(event)
        self._update_safety_from_structured_event(event, line)
        self._update_from_session_event(event, line)
        self._sync_parameter_state_from_session()
        self._refresh_parameter_tree()
        self._refresh_overview_parameter_tree()
        self._refresh_tuning_tree()

    def _append_log_line(self, line: str, tag: str | None = None) -> None:
        tagged_line = f"{datetime.now().strftime('%H:%M:%S')} {line}"
        log_tag = tag or self._line_tag(line)
        self.log.insert("end", tagged_line + "\n", log_tag)
        self.log.see("end")
        self.latest_event.set(line[:90])
        if self.transcript_handle is not None:
            self.transcript_handle.write(tagged_line + "\n")
            self.transcript_handle.flush()

    def _session_event_log_tag(self, event: TuningEvent) -> str:
        if event.type == "tx":
            return "TX"
        if event.type == "rx":
            return "RX"
        if event.type == "dat":
            return "DAT"
        if event.type in {"round", "decision", "baseline"}:
            return "ROUND"
        if event.type in {"error", "action_rejected"}:
            return "ERR"
        return "INFO"

    def _update_from_session_event(self, event: TuningEvent, line: str) -> None:
        if event.type == "state":
            next_state = event.data.get("next_state")
            if next_state:
                self.status.set(str(next_state))
            return
        if event.type == "dat":
            sample = event.data.get("sample")
            if isinstance(sample, dict):
                self._update_dat_tree(sample)
            return
        if event.type == "baseline":
            score = event.data.get("score")
            if score is not None:
                self.score.set(str(score))
                self._add_history_event("基线", f"score={score}")
            return
        if event.type == "round":
            round_index = event.data.get("round")
            if round_index is not None:
                self.current_round.set(str(round_index))
            key = event.data.get("key")
            trial_value = event.data.get("trial_value")
            if key is not None and trial_value is not None:
                self.parameter_trial_values[str(key)] = trial_value
            self.decision.set("试验")
            return
        if event.type == "decision":
            self._apply_decision_event(event, line)
            return
        if event.type == "record":
            record = event.data.get("record", {})
            detail = json.dumps(record, ensure_ascii=False) if isinstance(record, dict) else line
            self._add_history_event("记录", detail)
            return
        if event.type == "warning":
            self._add_history_event("警告", line)
            return
        if event.type == "error":
            self.status.set("error")
            self._increment_counter(self.failed_count)
            self._add_history_event("错误", line)
            return
        if event.type == "summary":
            summary = event.data.get("summary", {})
            if isinstance(summary, dict):
                self._apply_summary_event(summary)
            self._add_history_event("总结", line)
            return
        if event.type == "action_rejected":
            self._add_history_event("错误", line)
            return
        if event.type == "action":
            self._add_history_event("动作", line)
            return
        if event.type not in {"tx", "rx", "info"}:
            self._add_history_event("事件", f"{event.type}: {line}")

    def _apply_decision_event(self, event: TuningEvent, line: str) -> None:
        decision = str(event.data.get("decision", "")).lower()
        score = event.data.get("score")
        if score is not None:
            self.score.set(str(score))
        if decision == "accept":
            self.decision.set("接受")
            self._increment_counter(self.accepted_count)
            self._add_history_event("接受", line)
        elif decision == "hold":
            self.decision.set("保持")
            self._increment_counter(self.held_count)
            self._add_history_event("保持", line)
        elif decision == "rollback":
            self.decision.set("回滚")
            self._increment_counter(self.rollback_count)
            self._add_history_event("回滚", line)
        elif decision:
            self.decision.set(decision)
            self._add_history_event("决策", line)
        else:
            self._add_history_event("决策", line)

    def _apply_summary_event(self, summary: dict[str, Any]) -> None:
        final_score = summary.get("final_score")
        if final_score is not None:
            self.score.set(str(final_score))
        if "accepted" in summary:
            self.accepted_count.set(str(summary.get("accepted")))
        if "held" in summary:
            self.held_count.set(str(summary.get("held")))
        if "rolled_back" in summary:
            self.rollback_count.set(str(summary.get("rolled_back")))
        if "failed" in summary:
            self.failed_count.set(str(summary.get("failed")))
        stop_reason = summary.get("stop_reason")
        if stop_reason is not None:
            self.stop_reason.set(str(stop_reason))
        last_stable = summary.get("last_stable")
        if isinstance(last_stable, dict):
            self.parameter_last_stable_values.update({str(key): value for key, value in last_stable.items()})
        active_keys = summary.get("active_parameter_keys")
        if isinstance(active_keys, list):
            self.parameter_active_keys = [str(key) for key in active_keys]
        skipped_keys = summary.get("skipped_parameter_keys")
        if isinstance(skipped_keys, list):
            self.parameter_skipped_keys = {str(key) for key in skipped_keys}
        final_parameters = summary.get("final_parameters")
        if isinstance(final_parameters, dict):
            self._update_parameter_values(final_parameters)

    def _session_event_to_line(self, event: TuningEvent) -> str:
        if event.type in {"tx", "rx"}:
            return f"{event.type.upper()} {event.message}"
        if event.type == "dat":
            sample = event.data.get("sample")
            payload = json.dumps(sample, ensure_ascii=False) if isinstance(sample, dict) else event.message
            return f"DAT {payload}"
        if event.type == "baseline":
            score = event.data.get("score")
            return event.message or f"Baseline score: {score}"
        if event.type == "round":
            round_index = event.data.get("round", "-")
            key = event.data.get("key", "-")
            trial_value = event.data.get("trial_value")
            if trial_value is None:
                return event.message or f"Round {round_index}: {key}"
            return event.message or f"Round {round_index}: {key} -> {trial_value}"
        if event.type == "decision":
            decision = event.data.get("decision", "decision")
            score = event.data.get("score")
            if score is None:
                return event.message or str(decision)
            return event.message or f"{decision}: score={score}"
        if event.type == "summary":
            return f"Summary: {json.dumps(event.data.get('summary', {}), ensure_ascii=False, default=str)}"
        if event.type == "state":
            previous_state = event.data.get("previous_state", "-")
            next_state = event.data.get("next_state", "-")
            reason = event.data.get("reason", "-")
            return f"STATE {previous_state} -> {next_state} ({reason})"
        if event.type == "action":
            action = event.data.get("action", "action")
            return event.message or f"ACTION {action}"
        if event.type == "action_rejected":
            code = event.data.get("error_code") or event.data.get("code") or "action_rejected"
            return event.message or f"ERROR [{code}] action rejected"
        if event.type == "record":
            record = event.data.get("record", {})
            return f"RECORD {json.dumps(record, ensure_ascii=False, default=str)}"
        if event.type == "warning":
            return f"WARN {event.message}"
        if event.type == "error":
            code = event.data.get("error_code") or event.data.get("code") or "unknown"
            return f"ERROR [{code}] {event.message}"
        if event.message:
            return event.message
        if event.data:
            return f"{event.type}: {json.dumps(event.data, ensure_ascii=False, default=str)}"
        return event.type

    def _handle_session_error(self, exc: BaseException) -> None:
        self.status.set("error")
        self._handle_line(f"ERROR [session_worker] {exc}")
        self._add_history_event("错误", f"session_worker: {exc}")

    def _handle_session_done(self, exit_code: int | None) -> None:
        self.session_worker = None
        self.status.set("空闲" if exit_code == 0 else "错误")
        self._add_history_event("结束", f"TuningSession worker 退出码 {exit_code}")
        self._close_transcript_file()
        self._write_session_final_artifacts(exit_code)
        self._refresh_tuning_tree()

    def _handle_line(self, line: str) -> None:
        self._append_log_line(line)
        self._update_from_line(line)
        self._refresh_tuning_tree()

    def _line_tag(self, line: str) -> str:
        if "ERROR" in line or "failed" in line.lower():
            return "ERR"
        if " TX " in line or line.startswith("TX "):
            return "TX"
        if " RX " in line or line.startswith("RX "):
            return "RX"
        if " DAT " in line or line.startswith("DAT "):
            return "DAT"
        if line.startswith("Round") or line.startswith("第 ") or " accept" in line or "rollback" in line or "hold" in line:
            return "ROUND"
        return "INFO"

    def _update_from_line(self, line: str) -> None:
        lower_line = line.lower()
        tx_match = re.search(r"\bTX\s+(.+)", line)
        if tx_match:
            command = tx_match.group(1).strip()
            source = self._source_for_sent_command(command)
            self._set_last_sent_command(source, command, "已由文本输出发送")
            self._preview_after_sent_command(source)
        score_match = re.search(r"score=([-+]?\d+(?:\.\d+)?)", line)
        if score_match:
            self.score.set(score_match.group(1))
        baseline_match = re.search(r"Baseline score:\s*([-+]?\d+(?:\.\d+)?)", line)
        if baseline_match:
            self.score.set(baseline_match.group(1))
            self._add_history_event("基线", f"score={baseline_match.group(1)}")
        round_match = re.search(r"(?:Round|第)\s*(\d+)", line)
        if round_match:
            self.current_round.set(round_match.group(1))
            self._add_history_event("轮次", line)
        trial_match = re.search(r"(?:Round|第)\s*\d+\s*(?:轮)?[:：]\s*([A-Za-z0-9_.-]+)\s*->\s*([-+]?\d+(?:\.\d+)?)", line)
        if trial_match:
            self.parameter_trial_values[trial_match.group(1)] = trial_match.group(2)
            self._preview_set_command(trial_match.group(1), trial_match.group(2), "文本输出解析的发送前预览")
            self._refresh_parameter_tree()
        if lower_line.startswith("accept") or " accept:" in lower_line:
            self.decision.set("接受")
            self._increment_counter(self.accepted_count)
            self._add_history_event("接受", line)
        elif lower_line.startswith("rollback") or " rollback:" in lower_line:
            self.decision.set("回滚")
            self._increment_counter(self.rollback_count)
            self._add_history_event("回滚", line)
        elif lower_line.startswith("hold") or " hold:" in lower_line:
            self.decision.set("保持")
            self._increment_counter(self.held_count)
            self._add_history_event("保持", line)
        elif "Round" in line or line.startswith("第 "):
            self.decision.set("试验")
        if "ERROR" in line or "failed" in lower_line:
            self._increment_counter(self.failed_count)
            self._add_history_event("错误", line)
        summary_match = re.search(r"Summary:\s*({.*})", line)
        if summary_match:
            try:
                summary = json.loads(summary_match.group(1))
            except json.JSONDecodeError:
                summary = {}
            final_score = summary.get("final_score")
            if final_score is not None:
                self.score.set(str(final_score))
            if "accepted" in summary:
                self.accepted_count.set(str(summary.get("accepted")))
            if "held" in summary:
                self.held_count.set(str(summary.get("held")))
            if "rolled_back" in summary:
                self.rollback_count.set(str(summary.get("rolled_back")))
            if "failed" in summary:
                self.failed_count.set(str(summary.get("failed")))
            stop_reason = summary.get("stop_reason")
            if stop_reason is not None:
                self.stop_reason.set(str(stop_reason))
            final_parameters = summary.get("final_parameters")
            if isinstance(final_parameters, dict):
                self._update_parameter_values(final_parameters)
            self._add_history_event("总结", line)
        dat_match = re.search(r"\bDAT\s+({.*})", line)
        if dat_match:
            try:
                sample = json.loads(dat_match.group(1))
            except json.JSONDecodeError:
                return
            self._update_dat_tree(sample)
            self._update_safety_from_sample(sample)

    def _update_dat_tree(self, sample: dict[str, Any]) -> None:
        self.dat_tree.delete(*self.dat_tree.get_children())
        for index, (key, value) in enumerate(sample.items()):
            tag = "evenrow" if index % 2 == 0 else "oddrow"
            self.dat_tree.insert("", "end", text=str(key), values=(str(value),), tags=(tag,))
        self._record_waveform_sample(sample)

    def _record_waveform_sample(self, sample: dict[str, Any]) -> None:
        if not hasattr(self, "waveform_canvas"):
            return
        numeric_sample: dict[str, float] = {}
        for key, value in sample.items():
            try:
                number = float(value)
            except (TypeError, ValueError):
                continue
            if str(key).lower() in {"timestamp", "time", "t"}:
                continue
            numeric_sample[str(key)] = number
        if numeric_sample:
            self.waveform_samples.append(numeric_sample)
            self.waveform_samples = self.waveform_samples[-160:]
        self._draw_waveform()

    def _draw_waveform(self) -> None:
        if not hasattr(self, "waveform_canvas"):
            return
        canvas: tk.Canvas = self.waveform_canvas
        canvas.delete("all")
        width = max(int(canvas.winfo_width()), 640)
        height = max(int(canvas.winfo_height()), 260)
        margin_left = 46
        margin_right = 18
        margin_top = 26
        margin_bottom = 34
        plot_left = margin_left
        plot_right = width - margin_right
        plot_top = margin_top
        plot_bottom = height - margin_bottom

        for i in range(5):
            y = plot_top + (plot_bottom - plot_top) * i / 4
            canvas.create_line(plot_left, y, plot_right, y, fill="#2a3733")
        for i in range(6):
            x = plot_left + (plot_right - plot_left) * i / 5
            canvas.create_line(x, plot_top, x, plot_bottom, fill="#1d2825")
        canvas.create_line(plot_left, plot_bottom, plot_right, plot_bottom, fill="#70837d")
        canvas.create_line(plot_left, plot_top, plot_left, plot_bottom, fill="#70837d")

        if len(self.waveform_samples) < 2:
            canvas.create_text(
                width / 2,
                height / 2,
                text="等待 DAT",
                fill="#8fa19b",
                font=(self.ui_font, 12, "bold"),
            )
            return

        ordered_keys: list[str] = []
        for sample in self.waveform_samples:
            for key in sample:
                if key not in ordered_keys:
                    ordered_keys.append(key)
        keys = ordered_keys[:4]
        colors = [self.colors["accent"], self.colors["success"], self.colors["warning"], self.colors["danger"]]

        for key_index, key in enumerate(keys):
            values = [sample[key] for sample in self.waveform_samples if key in sample]
            if len(values) < 2:
                continue
            low = min(values)
            high = max(values)
            span = high - low if high != low else 1.0
            points: list[float] = []
            for index, sample in enumerate(self.waveform_samples):
                if key not in sample:
                    continue
                x = plot_left + (plot_right - plot_left) * index / max(len(self.waveform_samples) - 1, 1)
                normalized = (sample[key] - low) / span
                y = plot_bottom - normalized * (plot_bottom - plot_top)
                points.extend([x, y])
            color = colors[key_index % len(colors)]
            if len(points) >= 4:
                canvas.create_line(*points, fill=color, width=2, smooth=True)
            legend_x = plot_left + key_index * 130
            canvas.create_line(legend_x, 14, legend_x + 18, 14, fill=color, width=3)
            canvas.create_text(
                legend_x + 24,
                14,
                text=str(key),
                anchor="w",
                fill=self.colors["log_fg"],
                font=(self.ui_font, 9),
            )

    def _session_log_dir(self) -> Path:
        base = (
            Path(self.plan_path.get()).parent
            if self.plan_path.get().strip()
            else Path.home() / "AppData" / "Local" / "Temp"
        )
        return base / "mcu_tuning_logs"

    def _open_transcript_file(self, prefix: str = "desktop_panel") -> None:
        self._close_transcript_file()
        log_dir = self._session_log_dir()
        log_dir.mkdir(parents=True, exist_ok=True)
        path = log_dir / f"{prefix}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
        self.transcript_path.set(str(path))
        self.transcript_handle = path.open("a", encoding="utf-8")
        self._refresh_tuning_tree()

    def _close_transcript_file(self) -> None:
        if self.transcript_handle is not None:
            self.transcript_handle.close()
            self.transcript_handle = None

    def _prepare_session_artifacts(self, log_dir: Path) -> None:
        if self.session is None:
            return
        session_id = str(getattr(self.session, "session_id", "session"))
        log_dir.mkdir(parents=True, exist_ok=True)
        jsonl_path = log_dir / f"{session_id}.jsonl"
        snapshot_path = log_dir / f"{session_id}_plan_snapshot.yaml"
        summary_path = log_dir / f"{session_id}_summary.json"
        manifest_path = log_dir / f"{session_id}_session.json"
        transcript = self.transcript_path.get()
        self.current_session_artifacts = {
            "session_id": session_id,
            "plan_path": str(Path(self.plan_path.get()).resolve()) if self.plan_path.get().strip() else "",
            "session_log": str(jsonl_path),
            "transcript": transcript if transcript != "-" else "",
            "plan_snapshot": str(snapshot_path),
            "final_summary": str(summary_path),
            "manifest": str(manifest_path),
        }
        try:
            if self.plan_path.get().strip() and Path(self.plan_path.get()).exists():
                shutil.copy2(Path(self.plan_path.get()), snapshot_path)
            elif self.plan is not None:
                snapshot_path.write_text(json.dumps(self.plan, ensure_ascii=False, indent=2), encoding="utf-8")
            self._write_session_manifest("started")
        except OSError as exc:
            self._add_history_event("回放", f"会话工件写入失败：{exc}")

    def _write_session_manifest(self, status: str, exit_code: int | None = None) -> None:
        manifest_path = self.current_session_artifacts.get("manifest")
        if not manifest_path:
            return
        payload = {
            "schema_version": SESSION_REPLAY_SCHEMA_VERSION,
            "artifact_type": "tuning_session_manifest",
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "status": status,
            "session_id": self.current_session_artifacts.get("session_id"),
            "plan_path": self.current_session_artifacts.get("plan_path"),
            "last_exit_code": exit_code,
            "artifact_paths": {
                key: value
                for key, value in self.current_session_artifacts.items()
                if key not in {"session_id", "plan_path"} and value
            },
        }
        Path(manifest_path).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def _write_session_final_artifacts(self, exit_code: int | None) -> None:
        if self.session is None or not self.current_session_artifacts:
            return
        summary_path = self.current_session_artifacts.get("final_summary")
        if not summary_path:
            return
        try:
            summary = self.session.summary()
            payload = {
                "schema_version": SESSION_REPLAY_SCHEMA_VERSION,
                "artifact_type": "tuning_session_summary",
                "generated_at": datetime.now().isoformat(timespec="seconds"),
                "session_id": self.current_session_artifacts.get("session_id"),
                "plan_path": self.current_session_artifacts.get("plan_path"),
                "last_exit_code": exit_code,
                "summary": summary,
                "artifact_paths": {
                    key: value
                    for key, value in self.current_session_artifacts.items()
                    if key not in {"session_id", "plan_path"} and value
                },
            }
            Path(summary_path).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            self._write_session_manifest("finished", exit_code)
            self._add_history_event("回放", f"会话工件已保存：{self.current_session_artifacts.get('manifest')}")
        except OSError as exc:
            self._add_history_event("回放", f"会话工件写入失败：{exc}")

    def _dispatch_session_control(self, method_name: str, label: str, *args: Any, **kwargs: Any) -> Any:
        if self.session is None or not hasattr(self.session, method_name):
            self._add_history_event("错误", f"{label}: 当前没有可控 TuningSession")
            self._refresh_tuning_tree()
            return None
        method = getattr(self.session, method_name)
        result = method(*args, **kwargs)
        ok = bool(getattr(result, "ok", True))
        message = str(getattr(result, "message", "") or method_name)
        state = getattr(result, "state", None)
        if state is not None:
            self.status.set(str(state))
        if ok:
            self._add_history_event("动作", f"{label}: {message}")
        else:
            error_code = getattr(result, "error_code", None)
            if error_code is None:
                data = getattr(result, "data", {})
                if isinstance(data, dict):
                    error_code = data.get("error_code") or data.get("code")
            detail = f"{label}: {error_code or message}"
            self._add_history_event("错误", detail)
        self._refresh_tuning_tree()
        return result

    def pause_session(self) -> Any:
        return self._dispatch_session_control("pause", "暂停")

    def resume_session(self) -> Any:
        return self._dispatch_session_control("resume", "恢复")

    def skip_current_parameter(self) -> Any:
        return self._dispatch_session_control("skip_current_param", "跳过当前参数", reason="gui_operator")

    def rollback_to_last_stable(self) -> Any:
        return self._dispatch_session_control("rollback_to", "回滚到稳定值", "last_stable")

    def rollback_to_baseline(self) -> Any:
        return self._dispatch_session_control("rollback_to", "回滚到基线", "baseline")

    def stop(self) -> None:
        self.stop_requested = True
        if self.session_worker is not None and self.session_worker.is_alive():
            if self.session is not None and hasattr(self.session, "request_stop"):
                self.session.request_stop()
            self.status.set("停止中")
            self.stop_reason.set("手动停止")
            self._add_history_event("停止", "用户请求 TuningSession 停止")
            self._refresh_tuning_tree()
            return
        if self.proc is not None:
            try:
                self.proc.terminate()
            except Exception:
                pass
            self.proc = None
        self.status.set("已停止")
        self.stop_reason.set("手动停止")
        self._add_history_event("停止", "用户请求停止")
        self._close_transcript_file()
        self._refresh_tuning_tree()

    def emergency_stop(self) -> None:
        self.stop_requested = True
        if self.session_worker is not None and self.session_worker.is_alive():
            if self.session is not None and hasattr(self.session, "emergency_stop"):
                self.session.emergency_stop()
            self.status.set("急停")
            self.stop_reason.set("operator_abort")
            self._add_history_event("急停", "operator_abort")
            self._refresh_tuning_tree()
            return
        if self.session is not None and hasattr(self.session, "emergency_stop"):
            self.session.emergency_stop()
        if self.proc is not None:
            try:
                self.proc.terminate()
            except Exception:
                pass
            self.proc = None
        self.status.set("急停")
        self.stop_reason.set("operator_abort")
        self._add_history_event("急停", "operator_abort")
        self._close_transcript_file()
        self._refresh_tuning_tree()

    def clear_log(self) -> None:
        self.log.delete("1.0", "end")
        self.dat_tree.delete(*self.dat_tree.get_children())
        self.score.set("-")
        self.decision.set("-")
        self.current_round.set("-")
        self.accepted_count.set("0")
        self.held_count.set("0")
        self.rollback_count.set("0")
        self.failed_count.set("0")
        self.stop_reason.set("-")
        self.latest_event.set("-")
        self.parameter_baseline_values = {}
        self.parameter_trial_values = {}
        self.parameter_skipped_keys = set()
        self.safety_triggered_rules = set()
        self._populate_safety_tree(self.plan)
        self._reset_command_preview()
        if self.parameter_metadata_by_key:
            self.parameter_last_stable_values = dict(self.parameter_current_values)
            self._refresh_parameter_tree()
            self._refresh_overview_parameter_tree()
        if hasattr(self, "history_tree"):
            self.history_tree.delete(*self.history_tree.get_children())
        self._refresh_tuning_tree()

    def open_transcript(self) -> None:
        path = self.transcript_path.get()
        if not path or path == "-" or not Path(path).exists():
            messagebox.showinfo("记录文件", "当前还没有可打开的记录文件。")
            return
        subprocess.Popen(["notepad.exe", path])

    def destroy(self) -> None:
        self.disconnect_transport(record_history=False)
        self.stop()
        super().destroy()


def main(argv: list[str] | None = None) -> int:
    default_ui = os.environ.get("MCU_TUNING_PANEL_UI", "simple")
    if default_ui not in {"simple", "advanced"}:
        default_ui = "simple"
    parser = argparse.ArgumentParser(description="打开 MCU 蓝牙调参桌面面板。")
    parser.add_argument("--plan", type=Path, default=None, help="Optional mcu_tuning_plan.yaml to preload")
    parser.add_argument("--mode", choices=["demo", "monitor", "run"], default="demo")
    parser.add_argument(
        "--ui",
        choices=["simple", "advanced"],
        default=default_ui,
        help="Desktop UI mode: clean default console or the full multi-page console",
    )
    parser.add_argument(
        "--run-backend",
        choices=["session", "subprocess"],
        default=os.environ.get("MCU_TUNING_PANEL_RUN_BACKEND", "session"),
        help="Automatic tuning backend: direct TuningSession worker or legacy subprocess runner",
    )
    parser.add_argument("--auto-start", action="store_true", help="Start the selected mode after launch")
    parser.add_argument("--self-test", action="store_true", help="Import and initialize core classes, then exit")
    parser.add_argument("--hidden-init-test", action="store_true", help="Create a hidden Tk panel and exit")
    args = parser.parse_args(argv)

    if args.self_test:
        print("RESULT: tuning_panel self-test ok")
        return 0

    if args.hidden_init_test:
        app = TuningPanel(run_backend=args.run_backend, ui_mode=args.ui)
        app.withdraw()
        app.update_idletasks()
        app.destroy()
        print("RESULT: tuning_panel hidden init ok")
        return 0

    app = TuningPanel(auto_demo=args.auto_start and args.mode == "demo", run_backend=args.run_backend, ui_mode=args.ui)
    app.mode.set(args.mode)
    if args.plan is not None:
        app.plan_path.set(str(args.plan.resolve()))
        app.validate_current_plan()
        if args.auto_start and args.mode != "demo":
            app.after(300, app.start)
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
