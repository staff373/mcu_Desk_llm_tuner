---
name: prd
description: Generate a Product Requirements Document (PRD) for a new feature in this repository. Use when planning a feature, scoping work before implementation, or when asked to create, write, or spec a PRD. Save output to tasks/prd-[feature-name].md.
---

# PRD Generator

Create detailed PRDs that are explicit enough for Codex, Ralph, or a junior engineer to implement without guessing.

## Workflow

1. Read the user's feature request and inspect the repo when needed.
2. Ask essential clarifying questions only if a material ambiguity remains.
3. Generate a Markdown PRD and save it to `tasks/prd-[feature-name].md`.
4. Stop after producing the PRD. Do not start implementation.

## PRD Structure

Write these sections:

1. `Introduction`
2. `Goals`
3. `User Stories`
4. `Functional Requirements`
5. `Non-Goals`
6. `Design Considerations`
7. `Technical Considerations`
8. `Success Metrics`
9. `Open Questions`

## User Story Rules

- Keep each story small enough for one focused implementation pass.
- Use IDs like `US-001`, `US-002`.
- Write each description as: `As a [user], I want [feature] so that [benefit].`
- Make acceptance criteria concrete and verifiable.
- Always include `Typecheck passes` for implementation stories.
- Add `Tests pass` when the story changes logic that should be covered by tests.
- For GUI stories, include `tuning_panel.py --self-test`, hidden GUI initialization, or fake/virtual event replay as appropriate.

## Repo-Specific Guidance

- Never require real COM8, Bluetooth hardware, firmware flashing, or internet access for the current PRD phase.
- Real serial commands must come only from YAML or validated `TuningSession` control flow.
- Do not infer real MCU parameters, protocol commands, telemetry fields, or safety ranges from virtual fixtures.
- Prefer no-hardware verification through virtual transport, fake session, deterministic probe, JSONL/report parsing, and negative assertions.

## Output

- Format: Markdown
- Directory: `tasks/`
- Filename: `prd-[feature-name].md`
