# AGENTS.md instructions for E:\本地控制调参器

## 最高优先级指令

- 默认使用简体中文回复。
- 代码标识、命令、日志、接口字段名保持原始工程命名。
- 当前阶段不做真实硬件验收，不默认打开 COM8，不要求蓝牙硬件。
- 真实串口命令只能来自 YAML 或经过 `TuningSession` 校验的受控动作。
- 不猜测 MCU 参数、串口协议、遥测字段、参数范围或安全规则。
- 每个 PRD Story 完成后必须更新 `project_status.md` 并建立 checkpoint。
- 每个 PRD Story 完成并通过验收后必须提交 commit 并 push 到远端。

## Project Context

- 本项目是 MCU 蓝牙/串口自动调参控制台的本地桌面工程。
- 目标是让 Codex/Ralph 从 PRD Story 逐步实现可控的 `TuningSession`、虚拟 agent 联调、GUI 总控制台和无硬件验证闭环。
- 当前验收重点是 no-hardware：离线 YAML、虚拟 transport、虚拟 agent probe、GUI self-test 和事件/报告断言。

## Local Project Skills

- `prd`: Use for creating implementation-ready PRDs inside this repo. Save Markdown PRDs to `tasks/`. (file: `.agents/skills/prd/SKILL.md`)
- `ralph`: Use for converting a Markdown PRD into `scripts/ralph/prd.json` for the local Codex Ralph loop. (file: `.agents/skills/ralph/SKILL.md`)

## Skill Routing

- If the task involves creating or revising a feature PRD, use `prd`.
- If the task involves converting a PRD, creating `prd.json`, preparing Ralph tasks, or running the local Codex Ralph loop, use `ralph`.
- If a task spans planning and execution, use `prd` first, then `ralph`, then run `scripts/ralph/ralph-codex.ps1`.

## Ralph Workflow

- Active Ralph JSON lives at `scripts/ralph/prd.json`.
- Active Ralph progress lives at `scripts/ralph/progress.txt`.
- Older Ralph runs are archived under `scripts/ralph/archive/`.
- Run local Ralph with `powershell -ExecutionPolicy Bypass -File .\scripts\ralph\ralph-codex.ps1`.
- Each Ralph iteration must complete only one Story.
- Each completed Story must set its `passes` field to `true` in `scripts/ralph/prd.json`.
- Each completed Story must append a progress entry to `scripts/ralph/progress.txt`.
- Each completed Story must update `project_status.md` with verification and checkpoint information.

## Git Commit Conventions

- Commit titles should default to Chinese.
- Conventional Commit prefixes such as `feat:`, `fix:`, `docs:`, `refactor:`, `chore:`, and `test:` may remain in English.
- Ralph Story commits should use this format: `feat: 完成 [Story ID] [中文摘要]`.
- Do not commit broken code.
- Do not sweep unrelated dirty files into a Story commit.
