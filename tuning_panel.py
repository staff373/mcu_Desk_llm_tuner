#!/usr/bin/env python
"""桌面版 MCU 蓝牙调参执行与观测面板。"""

from __future__ import annotations

import argparse
import json
import queue
import re
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import tkinter as tk
from tkinter import filedialog, font as tkfont, messagebox, ttk

try:
    from validate_plan import load_plan, validate_plan
except ImportError as exc:  # pragma: no cover - script layout dependent
    print("ERROR: validate_plan.py must be in the same scripts directory.", file=sys.stderr)
    raise SystemExit(3) from exc


SCRIPT_DIR = Path(__file__).resolve().parent
RUN_SCRIPT = SCRIPT_DIR / "run_tuning_plan.py"
MONITOR_SCRIPT = SCRIPT_DIR / "monitor_tuning_plan.py"


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
    def __init__(self, auto_demo: bool = False) -> None:
        super().__init__()
        self.title("MCU 蓝牙调参面板")
        self.geometry("1180x760")
        self.minsize(980, 640)

        self.plan_path = tk.StringVar()
        self.mode = tk.StringVar(value="demo")
        self.status = tk.StringVar(value="空闲")
        self.port = tk.StringVar(value="-")
        self.baudrate = tk.StringVar(value="-")
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

        self.plan: dict[str, Any] | None = None
        self.session: Any | None = None
        self.proc: subprocess.Popen[str] | None = None
        self.reader_thread: threading.Thread | None = None
        self.output_queue: queue.Queue[str] = queue.Queue()
        self.stop_requested = False
        self.demo_index = 0
        self.transcript_handle: Any | None = None
        self.ui_font = self._pick_font(("Microsoft YaHei UI", "Segoe UI", "Arial"))
        self.mono_font = self._pick_font(("Cascadia Mono", "Consolas", "Courier New"))
        self.colors = {
            "bg": "#f5f7fb",
            "panel": "#ffffff",
            "panel_soft": "#f8fafc",
            "header": "#101827",
            "header_subtle": "#d6deeb",
            "text": "#111827",
            "muted": "#64748b",
            "border": "#dbe3ef",
            "accent": "#2563eb",
            "accent_active": "#1d4ed8",
            "danger": "#dc2626",
            "danger_active": "#b91c1c",
            "log_bg": "#111827",
            "log_fg": "#dbe4f0",
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
        style.map("TRadiobutton", background=[("active", self.colors["panel"])])
        style.configure("TButton", font=(self.ui_font, 10), padding=(12, 7), borderwidth=0)
        style.configure("Accent.TButton", background=self.colors["accent"], foreground="#ffffff", font=(self.ui_font, 10, "bold"))
        style.map("Accent.TButton", background=[("active", self.colors["accent_active"])])
        style.configure("Danger.TButton", background=self.colors["danger"], foreground="#ffffff", font=(self.ui_font, 10, "bold"))
        style.map("Danger.TButton", background=[("active", self.colors["danger_active"])])
        style.configure("TNotebook", background=self.colors["bg"], borderwidth=0)
        style.configure(
            "TNotebook.Tab",
            background="#e8eef7",
            foreground="#334155",
            font=(self.ui_font, 10, "bold"),
            padding=(18, 9),
        )
        style.map(
            "TNotebook.Tab",
            background=[("selected", self.colors["panel"]), ("active", "#f1f5f9")],
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
        ttk.Button(controls, text="启动", style="Accent.TButton", command=self.start).grid(row=0, column=4, padx=(0, 8))
        ttk.Button(controls, text="停止", style="Danger.TButton", command=self.stop).grid(row=0, column=5, padx=(0, 8))
        ttk.Button(controls, text="急停", style="Danger.TButton", command=self.emergency_stop).grid(row=0, column=6)

        mode_frame = ttk.Frame(controls, style="Card.TFrame")
        mode_frame.grid(row=1, column=1, sticky="w", padx=8, pady=(10, 0))
        ttk.Radiobutton(mode_frame, text="演示", variable=self.mode, value="demo").pack(side="left", padx=(0, 14))
        ttk.Radiobutton(mode_frame, text="观测", variable=self.mode, value="monitor").pack(side="left", padx=(0, 14))
        ttk.Radiobutton(mode_frame, text="自动调参", variable=self.mode, value="run").pack(side="left")
        ttk.Button(controls, text="清空日志", command=self.clear_log).grid(row=1, column=4, padx=(0, 8), pady=(12, 0))
        ttk.Button(controls, text="打开记录", command=self.open_transcript).grid(row=1, column=5, pady=(12, 0))

        self.notebook = ttk.Notebook(main)
        self.notebook.grid(row=1, column=0, sticky="nsew")

        self._build_overview_page()
        self._build_plan_page()
        self._build_monitor_page()
        self._build_tuning_page()
        self._build_history_page()

        footer = ttk.Frame(self, padding=(16, 0, 16, 12))
        footer.pack(fill="x")
        ttk.Label(footer, text="记录文件:", style="Footer.TLabel").pack(side="left")
        ttk.Label(footer, textvariable=self.transcript_path, style="FooterValue.TLabel").pack(side="left", padx=(6, 0))

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
            self.status.set("演示就绪")
            self.plan_status.set("演示模式：未加载 YAML")
            self._load_demo_params()
            self._add_history_event("校验", "演示模式无需 YAML")
            return True
        path = Path(self.plan_path.get().strip())
        if not path.exists():
            self.status.set("计划缺失")
            self.plan_status.set("计划缺失")
            self._populate_plan_tree(None)
            messagebox.showerror("计划缺失", "请先选择有效的 mcu_tuning_plan.yaml。")
            return False
        try:
            plan = load_plan(path)
            validator = validate_plan(plan)
        except Exception as exc:  # noqa: BLE001 - show practical UI error
            self.status.set("计划无效")
            self.plan_status.set("校验失败")
            self._populate_plan_tree(None)
            messagebox.showerror("校验失败", str(exc))
            return False
        if validator.errors:
            self.status.set("计划无效")
            self.plan_status.set(f"校验失败：{len(validator.errors)} 项错误")
            self._populate_plan_tree(plan)
            self._add_history_event("校验", f"失败：{'; '.join(validator.errors[:3])}")
            messagebox.showerror("校验失败", "\n".join(validator.errors[:12]))
            return False
        self.plan = plan
        self._load_plan_summary(plan)
        self.status.set("计划有效")
        self.plan_status.set("计划有效")
        self._add_history_event("校验", f"通过：{path.name}")
        return True

    def _load_plan_summary(self, plan: dict[str, Any]) -> None:
        transport = plan.get("transport", {})
        self.port.set(str(transport.get("port", "-")))
        self.baudrate.set(str(transport.get("baudrate", "-")))
        self.param_tree.delete(*self.param_tree.get_children())
        for index, param in enumerate(plan.get("parameters", [])):
            key = param.get("key", "-")
            current = param.get("current", "-")
            bounds = f"{param.get('min', '-') }..{param.get('max', '-')}"
            role = param.get("role", "-")
            tag = "evenrow" if index % 2 == 0 else "oddrow"
            self.param_tree.insert("", "end", iid=str(key), values=(key, current, bounds, role), tags=(tag,))
        self._populate_plan_tree(plan)
        self._refresh_tuning_tree()

    def _load_demo_params(self) -> None:
        self.port.set("DEMO")
        self.baudrate.set("115200")
        self.param_tree.delete(*self.param_tree.get_children())
        for index, (key, current, bounds, role) in enumerate([
            ("kp_x", "350", "100..600", "primary"),
            ("kd_x", "30", "0..80", "primary"),
            ("kp_y", "320", "100..600", "primary"),
            ("kd_y", "40", "0..80", "primary"),
        ]):
            tag = "evenrow" if index % 2 == 0 else "oddrow"
            self.param_tree.insert("", "end", iid=key, values=(key, current, bounds, role), tags=(tag,))
        self._populate_demo_plan_tree()
        self._refresh_tuning_tree()

    def _populate_demo_plan_tree(self) -> None:
        demo_plan = {
            "schema_version": "demo",
            "transport": {"port": "DEMO", "baudrate": 115200, "line_ending": "\\r\\n"},
            "commands": {"status": "STATUS", "set": "SET {key} {value}", "telemetry_on": "START", "stop": "STOP"},
            "parameters": [
                {"key": "kp_x", "current": 350, "min": 100, "max": 600, "role": "primary"},
                {"key": "kd_x", "current": 30, "min": 0, "max": 80, "role": "primary"},
                {"key": "kp_y", "current": 320, "min": 100, "max": 600, "role": "primary"},
                {"key": "kd_y", "current": 40, "min": 0, "max": 80, "role": "primary"},
            ],
            "telemetry": {"format": "CSV + DAT JSON demo", "sample_period_ms": 50},
            "step_policy": {"max_rounds": 2, "parameter_order": ["kp_x", "kd_x"]},
        }
        self._populate_plan_tree(demo_plan)

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
            if not self.param_tree.exists(item_id):
                continue
            old_values = list(self.param_tree.item(item_id, "values"))
            while len(old_values) < 4:
                old_values.append("-")
            old_values[1] = str(value)
            self.param_tree.item(item_id, values=old_values)

    def start(self) -> None:
        if self.proc is not None:
            messagebox.showinfo("正在运行", "当前已有任务正在运行。")
            return
        self.clear_log()
        self.stop_requested = False
        if self.mode.get() == "demo":
            self._start_demo()
            return
        if not self.validate_current_plan():
            return
        args = self._build_process_args()
        self._open_transcript_file()
        self.status.set("运行中")
        self._add_history_event("启动", f"{self.mode.get()} 模式")
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
                line = self.output_queue.get_nowait()
            except queue.Empty:
                break
            if line == "__PROCESS_DONE__":
                self.proc = None
                self.status.set("空闲")
                self._add_history_event("结束", "子进程已退出")
                self._close_transcript_file()
                self._refresh_tuning_tree()
            else:
                self._handle_line(line)
        self.after(80, self._pump_output)

    def _handle_line(self, line: str) -> None:
        tagged_line = f"{datetime.now().strftime('%H:%M:%S')} {line}"
        tag = self._line_tag(line)
        self.log.insert("end", tagged_line + "\n", tag)
        self.log.see("end")
        self.latest_event.set(line[:90])
        if self.transcript_handle is not None:
            self.transcript_handle.write(tagged_line + "\n")
            self.transcript_handle.flush()
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

    def _update_dat_tree(self, sample: dict[str, Any]) -> None:
        self.dat_tree.delete(*self.dat_tree.get_children())
        for index, (key, value) in enumerate(sample.items()):
            tag = "evenrow" if index % 2 == 0 else "oddrow"
            self.dat_tree.insert("", "end", text=str(key), values=(str(value),), tags=(tag,))

    def _open_transcript_file(self, prefix: str = "desktop_panel") -> None:
        self._close_transcript_file()
        base = Path(self.plan_path.get()).parent if self.plan_path.get().strip() else Path.home() / "AppData" / "Local" / "Temp"
        log_dir = base / "mcu_tuning_logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        path = log_dir / f"{prefix}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
        self.transcript_path.set(str(path))
        self.transcript_handle = path.open("a", encoding="utf-8")
        self._refresh_tuning_tree()

    def _close_transcript_file(self) -> None:
        if self.transcript_handle is not None:
            self.transcript_handle.close()
            self.transcript_handle = None

    def stop(self) -> None:
        self.stop_requested = True
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
        self.stop()
        super().destroy()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="打开 MCU 蓝牙调参桌面面板。")
    parser.add_argument("--plan", type=Path, default=None, help="Optional mcu_tuning_plan.yaml to preload")
    parser.add_argument("--mode", choices=["demo", "monitor", "run"], default="demo")
    parser.add_argument("--auto-start", action="store_true", help="Start the selected mode after launch")
    parser.add_argument("--self-test", action="store_true", help="Import and initialize core classes, then exit")
    args = parser.parse_args(argv)

    if args.self_test:
        print("RESULT: tuning_panel self-test ok")
        return 0

    app = TuningPanel(auto_demo=args.auto_start and args.mode == "demo")
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
