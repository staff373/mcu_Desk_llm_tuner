# Codex Ralph Instructions

You are running one iteration of the local Ralph loop for this repository.

## Your Task

1. Read `scripts/ralph/prd.json`.
2. Read `scripts/ralph/progress.txt`. Check the `## Codebase Patterns` section first if it exists.
3. Read `project_status.md` and follow its checkpoint requirements.
4. Check the target branch from `branchName`.
5. Switch to that branch before making changes. If it does not exist, create it from the current HEAD and switch to it.
6. Pick the highest-priority user story where `passes` is `false`.
7. Implement that single story only.
8. Run the relevant no-hardware quality checks for the surfaces you changed.
9. If you discover reusable repo knowledge, update `AGENTS.md` or `scripts/ralph/progress.txt` under `## Codebase Patterns`.
10. Update `scripts/ralph/prd.json` and set the completed story's `passes` field to `true`.
11. Append a progress entry to `scripts/ralph/progress.txt`.
12. Update `project_status.md` with Story status, verification evidence, changed files, and checkpoint path.
13. Create a checkpoint under `.project_checkpoints/US-XXX/YYYYMMDD-HHMMSS/` following `project_status.md`.
14. If checks pass, commit only the files required for this story, plus `scripts/ralph/prd.json`, `scripts/ralph/progress.txt`, and `project_status.md` when they changed. Do not sweep unrelated dirty files into the same commit. Use a Chinese commit message in this format: `feat: 完成 [Story ID] [中文摘要]`.
15. Push the Story commit to the configured remote.

## Progress Entry Format

Append to `scripts/ralph/progress.txt`:

```text
## [Date/Time] - [Story ID]
- What was implemented
- Files changed
- Verification commands and results
- Checkpoint path
- Real serial status: not opened / opened with user approval
- Learnings for future iterations:
  - Patterns discovered
  - Gotchas encountered
  - Useful context
---
```

If you discover a general pattern that future iterations should always know, add it near the top under:

```text
## Codebase Patterns
- Example pattern
```

Only add patterns that are broadly reusable.

## Quality Rules

- Do not work on more than one story in a single iteration.
- Do not commit broken code.
- Keep changes minimal and focused.
- Follow existing project conventions.
- Respect an already-dirty worktree. If unrelated local modifications exist, leave them untouched and stage only the files for the current story.
- Do not open COM8 or any real serial port unless a later user instruction explicitly moves the project into real-hardware validation.
- Do not add a free-form serial command path.
- Do not infer real MCU parameters, serial protocol, telemetry fields, or safety limits from virtual fixtures.

## Runtime Verification

Current PRD phase is no-hardware by default.

For backend/session stories, prefer:

- `python -m py_compile` for edited scripts and affected entrypoints
- deterministic fake/virtual transport tests
- JSONL/report parse-back checks
- negative assertions for invalid actions and no real serial open

For Tkinter GUI stories, verify with:

- `python tuning_panel.py --self-test`
- hidden GUI initialization when available
- fake-session dispatch or virtual event replay

For CLI compatibility, verify with:

- `python run_tuning_plan.py --help`
- `python monitor_tuning_plan.py --help`
- `python validate_plan.py <virtual_plan.yaml>`

If runtime verification is not available, note what still needs manual verification in `scripts/ralph/progress.txt` and `project_status.md`.

## Stop Condition

After completing one story, check whether all stories now have `passes: true`.

- If all stories are complete:
  - append a summary entry to `project_status.md`
  - commit and push the final bookkeeping
  - reply with exactly `<promise>COMPLETE</promise>`
- Otherwise end normally so the next iteration can continue.
