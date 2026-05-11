---
name: ralph
description: Convert a Markdown PRD into `scripts/ralph/prd.json` for the local Codex Ralph loop. Use when you already have a PRD and need dependency-ordered, right-sized user stories in Ralph JSON format.
---

# Ralph PRD Converter

Convert an existing PRD into the JSON task list used by the local Codex Ralph loop.

## Output Target

Write output to `scripts/ralph/prd.json`.

## Required JSON Shape

```json
{
  "project": "Project Name",
  "branchName": "ralph/feature-name",
  "description": "Short feature summary",
  "userStories": [
    {
      "id": "US-001",
      "title": "Story title",
      "description": "As a [user], I want [feature] so that [benefit].",
      "acceptanceCriteria": [
        "Criterion 1",
        "Typecheck passes"
      ],
      "priority": 1,
      "passes": false,
      "notes": ""
    }
  ]
}
```

## Conversion Rules

1. Each user story becomes one JSON entry.
2. Keep every story small enough for one Codex/Ralph iteration.
3. Order stories by dependency, then document order.
4. Set `passes` to `false` and `notes` to an empty string for every story.
5. Derive `branchName` as `ralph/[feature-name-kebab-case]`.

## Acceptance Criteria Rules

- Add `Typecheck passes` to every story.
- Add `Tests pass` when the story changes logic that should be validated by tests.
- For GUI stories, add a runtime verification criterion appropriate to Tkinter: `tuning_panel.py --self-test`, hidden initialization, fake-session dispatch, or virtual event replay.
- For no-hardware stories, include a negative assertion that COM8 or any real serial port was not opened.

## Archive Rule

Before overwriting `scripts/ralph/prd.json`, check whether the existing file belongs to a different `branchName`.

If it does, and `scripts/ralph/progress.txt` already contains iteration history:

- archive the current `prd.json`
- archive the current `progress.txt`
- write them to `scripts/ralph/archive/YYYY-MM-DD-feature-name/`

The local runner also performs this archive step automatically when branch state changes.

## Local Runner

Run the project-local Ralph loop with:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\ralph\ralph-codex.ps1
```

Use `-MaxIterations 1` for one Story iteration, or omit it to run until all stories pass.
