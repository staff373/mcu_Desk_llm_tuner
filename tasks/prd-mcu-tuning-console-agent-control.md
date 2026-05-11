# PRD: MCU Tuning Console Agent Control

## Introduction

当前工作区 `E:\本地控制调参器` 是 MCU 蓝牙/串口调参执行器的本地独立可运行副本，包含 `validate_plan.py`、`run_tuning_plan.py`、`monitor_tuning_plan.py`、`launch_tuning_window.py`、`tuning_session.py`、`tuning_panel.py` 和若干 `.cmd` 启动器。

现有系统已经具备 YAML 离线校验、真实串口自动调参 CLI、串口观测 CLI、Tkinter 中文桌面面板、演示模式、结构化事件和 JSONL 记录。当前主要缺口是：`TuningSession` 仍偏向一次性执行流程，GUI 自动调参仍主要通过子进程 stdout 解析状态，尚缺少可供 GUI 和 agent 共同调用的受控会话 API，也缺少不依赖真实 COM 口的 agent 连接与虚拟调参验证功能。

本 PRD 定义下一阶段目标：把当前观测面板升级为调参总控制台，并优先补齐底层 `TuningSession` 控制能力与虚拟 agent 联调测试。真实硬件参数、串口协议、遥测字段、参数范围和安全规则必须继续来自工程事实、固件协议、YAML 或用户确认，不能由 GUI、agent 或测试脚本猜测后用于真实设备。

## Goals

- 将控制台边界固定为：YAML 加载、YAML 校验、受控执行、人工干预、agent 控制、日志复盘。
- 将 `TuningSession` 升级为 GUI 和 agent 可直接控制的执行核心，支持明确状态机、暂停、继续、停止、跳参、回滚和运行时限制。
- 保留 `run_tuning_plan.py`、`monitor_tuning_plan.py`、`validate_plan.py` 的独立 CLI fallback 能力。
- 将 GUI 自动调参从子进程 stdout 解析逐步迁移到直接消费 `TuningSession` 结构化事件。
- 增加独立的 agent 连接测试与虚拟数据调参能力，证明 agent 能加载计划、发受控动作、接收结构化事件并导出报告。
- 在真实 COM8 实机验证前，所有新增自动化验证默认使用虚拟数据或离线 YAML，不默认打开真实串口。

## User Stories

### US-001: Session State Values

As a control-console developer, I want `TuningSession` to expose stable state values so that GUI and agent clients can render the current phase without parsing human text.

Acceptance criteria:

- `TuningSession` exposes state values for `idle`, `validating`, `connected`, `baseline`, `tuning`, `paused`, `rollback`, `stopping`, `stopped`, and `error`.
- State is readable without starting a real serial connection.
- Initial state is `idle` before any validation or transport creation.
- Terminal success state is `stopped`, and terminal failure state is `error`.
- Typecheck passes: `python -m py_compile tuning_session.py`.
- Tests pass for initial state, normal terminal state, and failure terminal state.

### US-002: State Transition Events

As an agent client, I want every state transition to emit a structured event so that automation can reconstruct the run timeline.

Acceptance criteria:

- Every state change emits event type `state`.
- Each `state` event includes `previous_state`, `next_state`, `reason`, `session_id`, and timestamp.
- State transitions are recorded in JSONL when logging is enabled.
- A no-hardware test can assert the exact transition sequence for a virtual successful run.
- Typecheck passes: `python -m py_compile tuning_session.py`.
- Tests pass for transition ordering and required event fields.

### US-003: Invalid Control Guardrails

As a console developer, I want invalid control calls to fail explicitly so that GUI and agent clients cannot silently corrupt session state.

Acceptance criteria:

- Calling `resume()` when not paused returns a structured rejected-action result.
- Calling `skip_current_param()` when no current parameter is active returns a structured rejected-action result.
- Calling `rollback_to(...)` before baseline exists returns a structured rejected-action result.
- Rejected actions emit an `error` or `action_rejected` event with `code`, `state`, and `action`.
- Typecheck passes: `python -m py_compile tuning_session.py`.
- Tests pass for each invalid control call.

### US-004: Pause And Resume

As an operator, I want to pause and resume a tuning session so that I can inspect state without ending the run.

Acceptance criteria:

- `pause()` moves the session to `paused` at a safe checkpoint and emits an action event.
- `resume()` returns the session to the previous runnable state and emits an action event.
- Pause does not send any YAML `set` command while paused.
- Pause and resume are testable using virtual telemetry without sleeping for real trial windows.
- Typecheck passes: `python -m py_compile tuning_session.py`.
- Tests pass for pause before baseline, pause during tuning, and resume after pause.

### US-005: Safe Stop

As an operator, I want stop to shut down telemetry safely so that the device is left in a predictable state.

Acceptance criteria:

- `request_stop()` moves the session toward `stopping` and then `stopped`.
- If a connection exists, stop attempts YAML-defined `telemetry_off` before YAML-defined `stop`.
- Stop emits `tx` events for attempted YAML stop commands.
- Stop records whether each stop command succeeded, timed out, or was unavailable.
- Typecheck passes: `python -m py_compile tuning_session.py`.
- Tests pass for stopping before connection, after connection, and during virtual tuning.

### US-006: Emergency Stop

As an operator, I want an emergency stop path so that a run can be marked as manually aborted and shutdown can be attempted immediately.

Acceptance criteria:

- Emergency stop is a separate action from normal stop and records `stop_reason` as an operator abort.
- Emergency stop does not send arbitrary serial text.
- Emergency stop uses only safe shutdown commands from YAML when a connection exists.
- Emergency stop is available to GUI and agent control layers through the same session API.
- Typecheck passes: `python -m py_compile tuning_session.py tuning_panel.py`.
- Tests pass for emergency stop state, event fields, and summary output.

### US-007: Skip Current Parameter

As an operator, I want to skip the current parameter so that one unstable axis or key does not block the full run.

Acceptance criteria:

- `skip_current_param()` skips only the currently active parameter.
- The skipped key and reason are emitted as an action event.
- Skipping does not mutate the source YAML file.
- Skipped parameters appear in the final summary and session report.
- Typecheck passes: `python -m py_compile tuning_session.py`.
- Tests pass for skip during tuning and rejected skip outside tuning.

### US-008: Rollback Targets

As an operator, I want to roll back to `last_stable` or `baseline` so that manual intervention can restore a known safe parameter set.

Acceptance criteria:

- `rollback_to("last_stable")` restores the current last stable values through YAML rollback command templates.
- `rollback_to("baseline")` restores baseline values through YAML rollback command templates.
- Rollback emits one action event and per-command `tx`/`rx` events.
- Rollback failure moves the session to `error` or a documented recoverable state.
- Typecheck passes: `python -m py_compile tuning_session.py`.
- Tests pass for both rollback targets, missing baseline rejection, and rollback command timeout.

### US-009: Active Parameter Selection

As an operator, I want to choose which parameters participate in this run so that I can tune one axis or a small parameter group.

Acceptance criteria:

- `set_active_parameters([...])` accepts only keys declared in YAML.
- Unknown keys are rejected before a run starts.
- Active parameter selection is session-scoped and does not mutate source YAML.
- Final summary lists active, skipped, and untouched parameters.
- Typecheck passes: `python -m py_compile tuning_session.py`.
- Tests pass for key filtering, unknown-key rejection, and no-mutation behavior.

### US-010: Runtime Limit Overrides

As an operator, I want to temporarily reduce runtime limits so that a supervised run can be shorter without weakening YAML safety limits.

Acceptance criteria:

- `set_runtime_limits(...)` can reduce `max_rounds`, `trial_window_ms`, and `cooldown_ms` for the current session.
- Overrides that exceed YAML limits are rejected before execution.
- Overrides are emitted as action events and recorded in JSONL.
- Source YAML remains unchanged after overrides.
- Typecheck passes: `python -m py_compile tuning_session.py`.
- Tests pass for allowed reductions, rejected expansions, and recorded override metadata.

### US-011: Standardized Error Events

As an agent client, I want execution failures to use stable machine-readable error fields so that automated control can distinguish recoverable failures from hard failures.

Acceptance criteria:

- Error events include `category`, `code`, `message`, `recoverable`, `state`, `action`, and optional context fields.
- Supported categories include `serial_exception`, `ok_timeout`, `readback_mismatch`, `malformed_telemetry`, `rollback_failed`, and `hard_failure`.
- Existing CLI output remains human-readable and includes the same error code where practical.
- Error records in JSONL preserve the same `category` and `code` as emitted events.
- Typecheck passes: `python -m py_compile tuning_session.py run_tuning_plan.py`.
- Tests pass for each standardized error type using fake or virtual transport inputs.

### US-012: Transport Interface

As a developer, I want a narrow transport interface so that real serial and virtual execution use the same session path.

Acceptance criteria:

- The transport interface supports write, readline, reset input buffer, close, and timeout behavior required by current session logic.
- Real serial behavior remains available through a pyserial-backed transport.
- Transport construction for real serial does not open COM8 during offline tests.
- `run_tuning_plan.py` keeps its existing real-serial default behavior and CLI arguments.
- Typecheck passes: `python -m py_compile tuning_session.py run_tuning_plan.py`.
- Tests pass for interface conformance and real transport construction without opening a port.

### US-013: Virtual Transport

As a tester, I want a virtual transport so that automatic tuning can be verified without real hardware.

Acceptance criteria:

- Virtual transport never opens COM8 or any real serial port.
- Virtual transport responds to the same YAML command templates used by the session core.
- Virtual transport emits realistic `rx` and `dat` behavior through the existing event pipeline.
- Virtual transport is opt-in through tests or probe scripts, never implicit for real tuning.
- Typecheck passes: `python -m py_compile tuning_session.py`.
- Tests pass for virtual STATUS, SET, telemetry_on, telemetry_off, stop, and rollback flows.

### US-014: Virtual Plan Fixture

As a tester, I want a virtual YAML plan fixture so that no-hardware verification is repeatable.

Acceptance criteria:

- Add a virtual plan fixture that validates with `validate_plan.py`.
- Virtual parameter keys include `kp_x`, `kd_x`, `kp_y`, and `kd_y`.
- Virtual telemetry includes `error_x`, `error_y`, `pwm_x`, `pwm_y`, `lost`, and `sat`.
- The fixture is clearly labeled test-only and cannot be mistaken for a real COM8 plan.
- Typecheck passes: `python -m py_compile validate_plan.py`.
- Tests pass: `python validate_plan.py <virtual_plan.yaml>` exits 0.

### US-015: Virtual Scoring Behavior

As a maintainer, I want virtual scoring to be deterministic but non-trivial so that accept, hold, and rollback paths are all exercised.

Acceptance criteria:

- Virtual telemetry improves as parameters approach deterministic target values.
- At least one scripted run produces an accepted trial.
- At least one scripted run produces hold or rollback.
- Re-running the same virtual test produces the same final summary unless the fixture changes.
- Typecheck passes: `python -m py_compile tuning_session.py`.
- Tests pass for deterministic score progression and expected decision counts.

### US-016: Agent Control Facade

As a Codex agent, I want a small control facade so that agent actions map to session APIs without reaching into GUI internals.

Acceptance criteria:

- The facade exposes `load_plan`, `validate_plan`, `start_auto_tune`, `pause`, `resume`, `skip_current_param`, `rollback_to`, `stop`, and `export_session_report`.
- The facade refuses actions before the plan is validated.
- The facade emits action results with `ok`, `action`, `state`, and optional `error_code`.
- The facade never exposes arbitrary serial text sending.
- Typecheck passes: `python -m py_compile tuning_session.py agent_console_probe.py`.
- Tests pass for allowed actions, rejected actions, and event forwarding.

### US-017: Agent Console Probe CLI

As a Codex agent, I want a standalone probe script so that agent-console connectivity can be verified in one command.

Acceptance criteria:

- Add `agent_console_probe.py` or an equivalent standalone script in the current script directory.
- The probe runs without COM8 and without opening any real serial port.
- The probe executes load, validate, start, pause, resume, skip, rollback, stop, and report export paths.
- The probe exits non-zero if required structured events are missing.
- Typecheck passes: `python -m py_compile agent_console_probe.py tuning_session.py validate_plan.py`.
- Tests pass: the probe exits 0 in a no-hardware environment and writes its artifacts.

### US-018: Session Report Export

As a maintainer, I want exported session reports so that virtual and real sessions can be reviewed after execution.

Acceptance criteria:

- Report export includes plan path, session id, action log, event counts, final summary, and generated artifact paths.
- Report export includes a statement that virtual runs did not open real serial ports.
- Report export can be generated after successful, stopped, and error sessions.
- Report schema is documented or enforced by a lightweight validation test.
- Typecheck passes: `python -m py_compile agent_console_probe.py tuning_session.py`.
- Tests pass for report schema, required fields, and artifact existence.

### US-019: GUI Session Worker

As an operator, I want the desktop panel to run automatic tuning through `TuningSession` directly so that the UI does not depend on parsing stdout.

Acceptance criteria:

- `tuning_panel.py` can start a `TuningSession` on a background thread for automatic tuning.
- The Tkinter main thread consumes events through a queue and never performs blocking serial reads.
- Existing subprocess mode remains available as fallback or compatibility path.
- Runtime verification: `python tuning_panel.py --self-test` passes, and hidden GUI initialization succeeds in a no-hardware environment.
- Typecheck passes: `python -m py_compile tuning_panel.py tuning_session.py`.
- Tests pass for direct-session worker lifecycle and fallback mode selection.

### US-020: GUI Event Rendering

As an operator, I want structured session events to update the correct GUI pages so that the panel reflects real session state.

Acceptance criteria:

- Monitor page displays `tx`, `rx`, and `dat` events from structured event data.
- Tuning page displays `baseline`, `round`, `decision`, and `summary` events from structured event data.
- History page displays `record`, `warning`, `error`, and `summary` events from structured event data.
- Unknown event types are recorded safely without crashing the UI.
- Runtime verification: GUI hidden initialization and a virtual event replay complete without errors.
- Typecheck passes: `python -m py_compile tuning_panel.py tuning_session.py`.
- Tests pass for event-to-page routing and unknown-event handling.

### US-021: GUI Operator Controls

As an operator, I want GUI controls for observation, automatic tuning, pause, resume, skip, rollback, stop, and emergency stop so that I can supervise tuning without editing files.

Acceptance criteria:

- GUI includes buttons for start monitor, start auto tune, pause, resume, skip current parameter, rollback to last stable, rollback to baseline, stop, and emergency stop.
- Buttons are enabled or disabled based on current session state.
- Buttons call only `TuningSession` control APIs or YAML-declared commands through the session.
- No free serial text box is added.
- Runtime verification: desktop GUI self-test and hidden initialization pass.
- Typecheck passes: `python -m py_compile tuning_panel.py tuning_session.py`.
- Tests pass for state-based button enablement and control API dispatch using a fake session.

### US-022: Connection Management Page

As an operator, I want a connection page so that serial configuration and connection state are visible before any real run starts.

Acceptance criteria:

- Connection view shows port, baudrate, connection state, detect ports, connect, and disconnect controls.
- Port detection does not auto-connect or send any serial command.
- Connection actions require a validated plan when using real transport.
- No self-test or hidden initialization opens COM8.
- Runtime verification: GUI hidden initialization opens the page without serial side effects.
- Typecheck passes: `python -m py_compile tuning_panel.py tuning_session.py`.
- Tests pass for no-auto-connect behavior and displayed YAML transport metadata.

### US-023: Parameter Page

As an operator, I want a parameter page so that current, baseline, last stable, and trial values are visible and selectable.

Acceptance criteria:

- Parameter view shows current value, baseline, last stable, trial value, range, step, and participation state.
- Parameter selection supports one axis, one parameter group, or specific keys for the current run only.
- Selection controls reject keys not declared in YAML.
- Selection changes do not mutate the source YAML file.
- Runtime verification: virtual plan can populate the parameter page.
- Typecheck passes: `python -m py_compile tuning_panel.py tuning_session.py`.
- Tests pass for metadata loading, selection filtering, and no-mutation behavior.

### US-024: Safety And Command Preview

As an operator, I want a safety and command preview view so that the next action is visible before the console sends YAML-derived commands.

Acceptance criteria:

- Safety view shows limits, stop conditions, communication timeout, and hard failure rules.
- Triggered safety rules are highlighted from structured events.
- Command preview shows the next YAML-derived command before it is sent.
- Command preview never accepts user-entered free serial text.
- Runtime verification: virtual session updates preview and safety state.
- Typecheck passes: `python -m py_compile tuning_panel.py tuning_session.py`.
- Tests pass for preview generation, safety event highlighting, and no free-text command path.

### US-025: Session Replay

As an operator, I want to replay historical sessions so that I can compare tuning outcomes after a run.

Acceptance criteria:

- Session management creates a session id and saves YAML snapshot, JSONL, transcript, and final summary.
- Replay view can load a historical session.
- Replay view compares baseline score, final score, accepted count, rollback count, and final parameter differences.
- Corrupt or partial session logs are reported as recoverable load errors.
- Runtime verification: a virtual session can be exported and loaded in replay view.
- Typecheck passes: `python -m py_compile tuning_panel.py tuning_session.py`.
- Tests pass for session metadata loading, replay summary parsing, and corrupt-log handling.

### US-026: Regression Verification Harness

As a maintainer, I want a repeatable no-hardware verification command or checklist so that future changes do not break CLI fallback, GUI startup, or virtual agent testing.

Acceptance criteria:

- Verification includes `python -m py_compile` for all relevant scripts.
- Verification includes `python validate_plan.py <virtual_plan.yaml>`.
- Verification includes `python run_tuning_plan.py --help`.
- Verification includes `python monitor_tuning_plan.py --help`.
- Verification includes `python tuning_panel.py --self-test`.
- Verification includes GUI hidden initialization.
- Verification includes virtual agent tuning probe.
- Verification includes a negative assertion that COM8 or any real serial port was not opened.
- Verification includes `quick_validate.py mcu-bluetooth-executor` when the global skill directory is in scope.
- Verification cleans `__pycache__` and temporary logs generated during the test run.
- Typecheck passes: all relevant scripts compile.
- Tests pass: no-hardware verification exits successfully and writes a concise verification summary.

## Functional Requirements

- FR-001: The control console must load a YAML tuning plan and validate it offline before any real serial connection is opened.
- FR-002: Real serial commands must originate only from YAML-declared commands, command templates, rollback templates, or validated session control flow.
- FR-003: GUI and agent clients must not expose arbitrary free-form serial command sending.
- FR-004: The session core must emit structured events for TX, RX, DAT, baseline, round, decision, record, summary, info, warning, error, and state changes.
- FR-005: The session core must provide a stable control API for pause, resume, stop, skip, rollback target selection, active parameter selection, and runtime limit reduction.
- FR-006: Runtime limit overrides must be bounded by YAML safety rules and must be recorded in the session log.
- FR-007: The virtual agent probe must execute without real hardware and must prove bidirectional control between an agent-like client and the session core.
- FR-008: Virtual telemetry must be deterministic enough for repeatable tests while still exercising accept, hold, and rollback decisions.
- FR-009: GUI automatic tuning must consume structured events directly when direct-session mode is enabled.
- FR-010: Existing CLI commands must remain usable after GUI and agent integration.
- FR-011: Session logs must include enough data for replay: plan snapshot, session id, state transitions, actions, TX/RX/DAT transcript, decisions, errors, and summary.
- FR-012: The console must make real-hardware execution explicit; no startup path, self-test, probe, or demo may default to COM8.
- FR-013: Every new control action must emit both a human-readable message and a machine-readable result or event.
- FR-014: Every virtual or test-only artifact must be labeled test-only and must not be usable as implicit evidence for real hardware protocol facts.
- FR-015: Every no-hardware verification path must include a negative proof that no real serial transport was opened.

## Verification Standards

- VS-001: Each implementation story must list the exact commands used for verification in the final handoff.
- VS-002: Typecheck means `python -m py_compile` must pass for every edited Python file plus directly affected entrypoints.
- VS-003: Logic changes require automated tests or deterministic probe coverage; manual visual checks alone are insufficient.
- VS-004: GUI changes require `tuning_panel.py --self-test`, hidden initialization, and at least one event replay or fake-session assertion.
- VS-005: Agent and virtual tuning changes require a no-hardware probe that exits non-zero when required events or report fields are missing.
- VS-006: Transport changes require a negative test proving real serial construction is not triggered during offline validation, self-test, or virtual probe.
- VS-007: Event changes require schema assertions for required fields, not only line-by-line stdout inspection.
- VS-008: JSONL or report changes require parsing the generated artifact back and asserting required fields.
- VS-009: CLI compatibility changes require `run_tuning_plan.py --help`, `monitor_tuning_plan.py --help`, and `validate_plan.py` on the virtual fixture.
- VS-010: Safety-boundary changes require negative tests for unknown parameter keys, over-limit runtime overrides, invalid rollback target, and free-form serial command rejection.
- VS-011: A verification run must not require COM8, Bluetooth hardware, firmware flashing, or internet access.
- VS-012: Temporary files, logs, and `__pycache__` created by verification must be cleaned or clearly listed as retained artifacts.

## Non-Goals

- Do not perform real COM8 validation in this phase.
- Do not infer real MCU parameters, protocol commands, telemetry fields, or safety ranges from virtual fixtures.
- Do not add a free-form serial terminal to the GUI.
- Do not remove or replace `run_tuning_plan.py` as an independent CLI fallback.
- Do not modify the global skill directory unless the implementation task explicitly includes syncing the local copy back to `C:\Users\1\.codex\skills\mcu-bluetooth-executor`.
- Do not broaden into firmware flashing, Bluetooth pairing automation, model-based PID design, or hardware-in-the-loop CI.

## Design Considerations

- The GUI should read as a workbench, not a passive log viewer: status, current action, next command, active parameter, and safety state should be visible without digging through logs.
- Control buttons should be state-aware to reduce accidental invalid operations.
- The command preview should show only YAML-derived commands and should explain why a command is pending.
- Error presentation should separate operator-facing text from machine-readable event fields.
- Virtual agent test output should be concise enough to run during handoff but detailed enough to prove event receipt and action dispatch.
- Historical replay should emphasize comparisons that help tuning decisions: baseline versus final score, accepted versus rolled back trials, and parameter deltas.

## Technical Considerations

- Current directory is a local script copy, not a git repository; implementation should preserve original and derived artifacts separately.
- `tuning_session.py` is the correct ownership point for serial I/O, telemetry parsing, scoring, rollback, structured events, and future transport abstraction.
- `tuning_panel.py` currently imports `subprocess` and starts `run_tuning_plan.py` or `monitor_tuning_plan.py`; direct-session mode should be introduced without deleting fallback behavior.
- The virtual transport should reuse the same session control path where possible, otherwise the probe may only test mocks instead of the real console interface.
- Validation should remain based on `validate_plan.py`; the virtual plan should satisfy the same schema used for real executor YAML.
- JSONL logging should remain append-only and should avoid mixing human-only text with machine-readable event fields.
- Threading in the GUI should keep all Tkinter widget updates on the main thread through an event queue.
- Any temporary override of parameters or limits must be session-scoped and must not mutate the source YAML file.
- The eventual skill sync should account for both this local directory and `C:\Users\1\.codex\skills\mcu-bluetooth-executor`.

## Success Metrics

- A no-hardware verification run validates the virtual YAML, runs the virtual agent probe, compiles scripts, self-tests the GUI, and proves no real serial port was opened.
- The virtual agent probe records at least one structured event of each required class used by the test path: action, state, tx, rx, dat, baseline, round, decision, summary, warning or recoverable error.
- A virtual session completes with deterministic final summary, parseable JSONL, and a report that passes required-field validation.
- Negative tests pass for invalid actions, invalid parameter keys, over-limit runtime overrides, missing baseline rollback, and free-form serial command rejection.
- CLI help for `run_tuning_plan.py` and `monitor_tuning_plan.py` remains unchanged or backwards compatible.
- GUI remains responsive during direct-session automatic tuning and can pause, resume, stop, skip, and rollback through session APIs.
- Real-hardware execution remains opt-in and YAML-bound.

## Open Questions

- Should the transport abstraction be introduced as a separate module, for example `tuning_transport.py`, or kept inside `tuning_session.py` until it stabilizes?
- Should agent control be exposed only as a Python API and probe script, or should it also include a local IPC interface for future external agents?
- What exact report format is preferred for `export_session_report`: Markdown, JSON, HTML, or all three?
- Should GUI replay load only sessions created by this console, or also import older JSONL logs created by `run_tuning_plan.py`?
- When syncing back to the global skill directory, should the local directory remain the development source of truth or become a generated/export copy?
