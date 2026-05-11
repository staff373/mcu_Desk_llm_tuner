# Project Status: MCU Tuning Console Agent Control

Last updated: 2026-05-11

Source PRD: `tasks/prd-mcu-tuning-console-agent-control.md`

Ralph execution PRD: `scripts/ralph/prd.json`

Ralph progress: `scripts/ralph/progress.txt`

Workspace: `E:\本地控制调参器`

Current policy: no real hardware acceptance in this phase. Do not open COM8, do not require Bluetooth hardware, and do not infer real MCU protocol facts from virtual fixtures.

## Purpose

This file is the project execution ledger for the PRD stories. Every story must update this file when it starts, when it finishes, and when verification or rollback state changes.

Here, "工程状态回档" means creating a restorable project checkpoint after each story, not automatically reverting the completed work. Completed code stays in place unless the user explicitly asks to restore a checkpoint.

## Status Values

| Status | Meaning |
| --- | --- |
| `todo` | Not started. |
| `in_progress` | Implementation or verification is active. |
| `blocked` | Waiting on a decision, dependency, or missing fact. |
| `implemented` | Code or document change is complete, verification not fully done. |
| `verified` | Story meets its acceptance criteria and verification evidence is recorded. |
| `checkpointed` | Story is verified and a rollback checkpoint was recorded. |
| `deferred` | Intentionally postponed. |

## Required Story Completion Gate

Each story is not considered complete until all items below are recorded.

1. Status is updated in the Story Board.
2. Changed files are listed in the Story Execution Log.
3. Verification commands and decisive results are recorded.
4. Generated artifacts, logs, JSONL, reports, or fixtures are listed.
5. A rollback checkpoint path is recorded.
6. The checkpoint includes enough information to restore edited files if the user later approves rollback.
7. No-hardware stories include a negative statement that no real serial port was opened.

## Checkpoint Rules

- Checkpoint root: `.project_checkpoints/`
- Checkpoint naming: `.project_checkpoints/US-XXX/YYYYMMDD-HHMMSS/`
- Each checkpoint should contain `before/`, `after/`, and `manifest.md`.
- `before/` stores copies or hashes of files before the story edits.
- `after/` stores copies or hashes of files after verification.
- `manifest.md` records story id, timestamps, edited files, verification commands, artifacts, and known rollback notes.
- Only checkpoint files touched by the story. Do not copy unrelated user directories, personal files, credential stores, or global system state.
- If a story creates new files, record them as removable artifacts in `manifest.md`.
- If a story changes global skill files under `C:\Users\1\.codex\skills\mcu-bluetooth-executor`, record that explicitly.
- Restoring a checkpoint is a destructive file operation and requires explicit user approval.

## Recommended Checkpoint Commands

Use these as examples; adjust the file list to the story's actual write set.

```powershell
$story = "US-001"
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$root = ".project_checkpoints\$story\$stamp"
New-Item -ItemType Directory -Force "$root\before", "$root\after" | Out-Null
```

Before edits, copy only planned write targets:

```powershell
Copy-Item -LiteralPath "tuning_session.py" -Destination "$root\before\tuning_session.py"
Get-FileHash -Algorithm SHA256 "tuning_session.py" | Format-List | Out-File "$root\before\hashes.txt"
```

After verification, copy changed files and write the manifest:

```powershell
Copy-Item -LiteralPath "tuning_session.py" -Destination "$root\after\tuning_session.py"
Get-FileHash -Algorithm SHA256 "tuning_session.py" | Format-List | Out-File "$root\after\hashes.txt"
```

## Story Board

| ID | Scope | Status | Verification Floor | Checkpoint |
| --- | --- | --- | --- | --- |
| US-001 | Session state values | checkpointed | `python -m py_compile tuning_session.py test_tuning_session_state.py`; `python -m unittest test_tuning_session_state.py` passed | `.project_checkpoints/US-001/20260511-121400/` |
| US-002 | State transition events | checkpointed | `python -m py_compile tuning_session.py test_tuning_session_state.py test_tuning_session_state_events.py`; `python -m unittest test_tuning_session_state.py test_tuning_session_state_events.py` passed | `.project_checkpoints/US-002/20260511-121738/` |
| US-003 | Invalid control guardrails | checkpointed | `python -m py_compile tuning_session.py test_tuning_session_state.py test_tuning_session_state_events.py test_tuning_session_invalid_controls.py`; `python -m unittest test_tuning_session_invalid_controls.py test_tuning_session_state.py test_tuning_session_state_events.py` passed | `.project_checkpoints/US-003/20260511-122342/` |
| US-004 | Pause and resume | checkpointed | `python -m py_compile tuning_session.py test_tuning_session_state.py test_tuning_session_state_events.py test_tuning_session_invalid_controls.py test_tuning_session_pause_resume.py`; `python -m unittest test_tuning_session_pause_resume.py test_tuning_session_invalid_controls.py test_tuning_session_state.py test_tuning_session_state_events.py` passed | `.project_checkpoints/US-004/20260511-123000/` |
| US-005 | Safe stop | todo | Stop sequencing tests with YAML telemetry_off and stop commands | pending |
| US-006 | Emergency stop | todo | Operator-abort summary and no free serial text path | pending |
| US-007 | Skip current parameter | todo | Skip during tuning and rejected skip outside tuning | pending |
| US-008 | Rollback targets | todo | last_stable, baseline, missing baseline, timeout tests | pending |
| US-009 | Active parameter selection | todo | Unknown key rejection and source YAML no-mutation test | pending |
| US-010 | Runtime limit overrides | todo | Allowed reductions, rejected expansions, metadata logging | pending |
| US-011 | Standardized error events | todo | Error schema tests for each required category | pending |
| US-012 | Transport interface | todo | Interface conformance and no port open during construction | pending |
| US-013 | Virtual transport | todo | Virtual STATUS, SET, telemetry, stop, rollback flows | pending |
| US-014 | Virtual plan fixture | todo | `validate_plan.py <virtual_plan.yaml>` exits 0 | pending |
| US-015 | Virtual scoring behavior | todo | Deterministic accept and rollback or hold outcomes | pending |
| US-016 | Agent control facade | todo | Allowed/rejected actions and event forwarding tests | pending |
| US-017 | Agent console probe CLI | todo | Probe exits 0 and fails on missing required events | pending |
| US-018 | Session report export | todo | Report schema and artifact existence checks | pending |
| US-019 | GUI session worker | todo | Worker lifecycle, fallback selection, GUI self-test | pending |
| US-020 | GUI event rendering | todo | Event-to-page routing and unknown-event handling | pending |
| US-021 | GUI operator controls | todo | Button enablement and fake-session dispatch tests | pending |
| US-022 | Connection management page | todo | No auto-connect and no COM8 during hidden init | pending |
| US-023 | Parameter page | todo | Metadata loading, selection filtering, no YAML mutation | pending |
| US-024 | Safety and command preview | todo | Preview generation, safety highlighting, no free-text path | pending |
| US-025 | Session replay | todo | Replay parsing and corrupt-log recoverable error handling | pending |
| US-026 | Regression verification harness | todo | Full no-hardware verification with no real serial proof | pending |

## Current Baseline

| Item | State |
| --- | --- |
| Project type | Git repository with project-local Ralph workspace |
| PRD exists | yes |
| Ralph workspace | `scripts/ralph/` |
| Ralph execution PRD | `scripts/ralph/prd.json` |
| Ralph progress log | `scripts/ralph/progress.txt` |
| Status ledger exists | yes |
| Real hardware required | no |
| Real COM8 validation | deferred |
| CLI fallback policy | preserve `run_tuning_plan.py`, `monitor_tuning_plan.py`, and `validate_plan.py` |
| Safety policy | real serial commands must come only from YAML or validated session control flow |

## Story Execution Log

| Date | Story | Status | Changed Files | Verification | Checkpoint | Notes |
| --- | --- | --- | --- | --- | --- | --- |
| 2026-05-11 | STATUS-001 | checkpointed | `project_status.md` | File created and aligned with PRD story list | not required for status bootstrap | Establishes per-story checkpoint and rollback rules |
| 2026-05-11 | STATUS-002 | verified | `AGENTS.md`, `.agents/skills/prd/SKILL.md`, `.agents/skills/ralph/SKILL.md`, `scripts/ralph/*`, `README.md`, `project_status.md` | Project-local Ralph workspace created; `scripts/ralph/prd.json` validated as JSON | not required for Ralph workspace migration | Aligns current project with the `medial_system_demo` local Ralph layout |
| 2026-05-11 | US-001 | checkpointed | `tuning_session.py`, `test_tuning_session_state.py`, `scripts/ralph/prd.json`, `scripts/ralph/progress.txt`, `project_status.md`, `.project_checkpoints/US-001/20260511-121400/` | `python -m py_compile tuning_session.py test_tuning_session_state.py` passed; `python -m unittest test_tuning_session_state.py` passed with 3 tests | `.project_checkpoints/US-001/20260511-121400/` | Added stable session states and no-hardware fake-serial tests; real serial status: not opened |
| 2026-05-11 | US-002 | checkpointed | `tuning_session.py`, `test_tuning_session_state_events.py`, `scripts/ralph/prd.json`, `scripts/ralph/progress.txt`, `project_status.md`, `.project_checkpoints/US-002/20260511-121738/` | `python -m py_compile tuning_session.py test_tuning_session_state.py test_tuning_session_state_events.py` passed; `python -m unittest test_tuning_session_state.py test_tuning_session_state_events.py` passed with 4 tests | `.project_checkpoints/US-002/20260511-121738/` | Added structured state transition events and JSONL state records; real serial status: not opened |
| 2026-05-11 | US-003 | checkpointed | `tuning_session.py`, `test_tuning_session_invalid_controls.py`, `scripts/ralph/prd.json`, `scripts/ralph/progress.txt`, `project_status.md`, `.project_checkpoints/US-003/20260511-122342/` | `python -m py_compile tuning_session.py test_tuning_session_state.py test_tuning_session_state_events.py test_tuning_session_invalid_controls.py` passed; `python -m unittest test_tuning_session_invalid_controls.py test_tuning_session_state.py test_tuning_session_state_events.py` passed with 7 tests | `.project_checkpoints/US-003/20260511-122342/` | Added structured rejected-action results and `action_rejected` events for invalid controls; real serial status: not opened |
| 2026-05-11 | US-004 | checkpointed | `tuning_session.py`, `test_tuning_session_pause_resume.py`, `scripts/ralph/prd.json`, `scripts/ralph/progress.txt`, `project_status.md`, `.project_checkpoints/US-004/20260511-123000/` | `python -m py_compile tuning_session.py test_tuning_session_state.py test_tuning_session_state_events.py test_tuning_session_invalid_controls.py test_tuning_session_pause_resume.py` passed; `python -m unittest test_tuning_session_pause_resume.py test_tuning_session_invalid_controls.py test_tuning_session_state.py test_tuning_session_state_events.py` passed with 9 tests | `.project_checkpoints/US-004/20260511-123000/` | Added structured pause/resume actions and safe pause checkpoints before baseline and trial SET; real serial status: not opened |

## Open Decisions

| ID | Decision Needed | Owner | Status |
| --- | --- | --- | --- |
| D-001 | Whether the transport abstraction should be a new module or remain in `tuning_session.py` initially | user or implementing agent | open |
| D-002 | Whether agent control should remain Python-only or later expose local IPC | user or implementing agent | open |
| D-003 | Preferred report format for `export_session_report` | user or implementing agent | open |
| D-004 | Whether local directory or global skill directory is the source of truth after implementation | user | open |

## Final Handoff Requirements

At the end of each implementation pass, the agent must report:

1. Stories changed.
2. Files changed.
3. Verification commands run.
4. Verification result summary.
5. Checkpoint path.
6. Whether any real serial port was opened.
7. Remaining blockers or risks.
