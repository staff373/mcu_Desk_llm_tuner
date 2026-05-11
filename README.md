# MCU Desk LLM Tuner

## 工程协作规则

1. 每个 PRD Story 完成后，必须更新 `project_status.md`，并按其中规则建立工程状态回档 checkpoint。
2. 每个 PRD Story 完成并通过验收后，必须提交 Git commit，并 push 到远端仓库。

## Ralph 工作区

- Markdown PRD 放在 `tasks/`。
- Ralph 执行文件放在 `scripts/ralph/prd.json`。
- Ralph 进度记录放在 `scripts/ralph/progress.txt`。
- Ralph 本地执行说明放在 `scripts/ralph/CODEX.md`。
- 本项目使用项目内 Ralph 工作区，不使用根目录 `prd.json` 作为执行入口。
