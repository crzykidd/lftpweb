---
name: 2026-08-21-poll-cadence-labelling
status: completed        # pending | completed | failed
created: 2026-08-21
model: sonnet            # small frontend/wording change
completed: 2026-08-21
result: >
  Renamed Settings -> Integrations' *arr poll card from "Poll cadence" to "How often to check
  Sonarr/Radarr", added a lead-in line stating it applies to every enabled instance (kept at the
  top of the page, not moved), relabeled the field "Check interval (seconds)", and moved the
  consequence explanation (Preflight progress, ~2x-interval import-detection lag, per-instance
  request cost) into a FieldHelp popover reused from the rest of the page, replacing the
  floor/default-only paragraph. Wording and placement only -- no change to poll_interval_s, its
  10s default, 5s floor, 3600s ceiling, or the API/validation. 1685 backend / 680 frontend tests,
  0 skipped, all four gates green. Not committed -- prepared the tree for the orchestrating
  session to commit.
---

# Task: make the *arr poll setting findable and explain what it does

Finding **6** of `prompts/test-findings-2026-08-21.md`. The user, who asked for this feature and
knew it had shipped, went looking for it and could not find it:

> *"I don't know what the poll cadence setting is or where to find it."*

It is at **Settings → Integrations**, top card, headed **"Poll cadence"**, field *"Poll interval
(seconds)"* (`frontend/src/pages/settings/IntegrationsTab.tsx`, `60f174f`).

## Two separable defects

**1. The name says nothing about what is polled.** "Poll cadence" is internal vocabulary. Neither
the heading nor the field label mentions Sonarr or Radarr, so even on a page full of *arr
configuration it reads as unrelated plumbing. Rename it to name the action *and* its target —
something like **"How often to check Sonarr/Radarr"**. Use the same *arr vocabulary the rest of the
page already uses; don't invent a third phrasing.

**2. It does not say what changing it affects.** The help text today explains the **floor and the
default** — mechanism, not consequence. What a user needs to know is what gets *faster*:

- how smoothly a **Preflight** row's progress ticks (it moves once per poll), and
- how quickly a finished item leaves **"Awaiting import"** — which needs *two* consecutive polls to
  confirm, so the observed lag is roughly twice the interval.

…and what it costs: more requests to the *arr, growing with how many instances are enabled.

Both are the exact symptoms issue #16 was opened about, so the help text should describe the thing
the user actually experiences rather than the knob's bounds.

**Reuse `FieldHelp.tsx`** — it exists for this and is already used elsewhere in Settings. Prefer it
over growing the paragraph under the field.

## Also worth fixing while you are there

**Placement.** "Top card" was the implementing agent's choice. A **site-wide** cadence knob sitting
above the per-instance list may read as belonging to the *first instance* rather than to all of
them. Look at the page as a whole and decide where a site-level setting belongs relative to the
instance list; if you move it, say why. If you keep it where it is, make it visually unambiguous
that it applies to every instance.

## Scope

**Wording, help, and placement only.** No behaviour change: the 10 s default, the 5 s floor, the
3600 s ceiling, the API and its validation all stay exactly as they are. If you find yourself
editing `core/arrsync.py`, stop — that is out of scope for this task.

## Before you start

- `frontend/src/pages/settings/IntegrationsTab.tsx` — the card as it stands.
- `frontend/src/components/FieldHelp.tsx` and an existing call site, for the idiom.
- `prompts/done/2026-08-21-arr-poll-cadence.md` — what shipped and why 10 s.

## Working tree check

Run `git status --porcelain` and cross-reference. If a file this plan touches is dirty, list it and
ask before proceeding. This prompt file is exempt.

## Tests

Likely none — this repo has no component-rendering harness and no Settings-tab component has
dedicated tests. Follow that convention rather than introducing a harness for a wording change. If
`docMarkdown.test.ts`-style section counting or any snapshot notices the change, update it.

## Docs

`CHANGELOG.md` — a short entry; this is user-visible even though it is only wording.
`docs/concepts.md` if it mentions the poll interval. Mark finding 6 **done** in
`prompts/test-findings-2026-08-21.md`. Append a one-line entry to `prompts/startnewsession.md`'s
"On `dev` since the release" — same commit.

## Conventions to honor

- **Never background a verification gate.** Foreground, `timeout` 600000 ms for pytest (~4 min),
  read each exit code. A spawned agent receives no background completion notification and will stall
  forever — a written rule in `CLAUDE.md`.
- From the **repo root** (not `backend/`): `uv run pytest`, `uv run ruff check .`,
  `uv run ruff format --check .`. From `frontend/`: `npm run lint`, `npx tsc -b`, `npm test -- --run`.
- Report backend and frontend test counts before and after; confirm 0 skipped. Prefix `fix:` (this
  corrects a usability defect rather than adding a feature). No `Co-authored-by:`.
- **You cannot render a page.** Say what a human should check.

## When done

1. Update frontmatter: `status`, `completed`, `result`.
2. `git mv` into `prompts/done/` (plain `mv` if git refuses — it is untracked).
3. **You are a spawned agent: do NOT commit.** Prepare the tree, report the file list and a proposed
   one-line commit message. Never `git add -A`, never push.
