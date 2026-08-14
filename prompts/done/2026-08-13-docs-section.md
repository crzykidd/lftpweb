---
name: 2026-08-13-docs-section
status: done
created: 2026-08-13
model: opus
completed: 2026-08-13
result: >
  Added a Docs section (left nav) with Quick start and Concepts tabs, written as React
  components — no markdown-renderer dependency. Quick start walks the six-step first-run path
  with every step linking to the settings page it describes; Concepts covers the settle gate,
  auto-queue suppression, a Dismiss/Clear-history/Reset-item-tracking blast-radius table, the
  lifecycle icons, copy vs move, and inherit-vs-override. Every factual claim was verified
  against the code first. Built `FieldHelp` (portal popover reusing the f4a4205 hover card's
  placement via a new shared `lib/popoverPosition.ts`, click/keyboard/touch reachable) and
  demonstrated it on three fields: Sync mode, Patterns-only, Known-hosts policy. `Layout.tsx`'s
  hardcoded settings-tab check replaced with `nav.ts.tabsForPath`. Also corrected one stale
  paragraph on Settings → Post-processing that still described the pre-`3500b3f` AND. 887
  backend tests unchanged; frontend 105 → 118; lint/build clean. Not verified: nobody has seen
  any of it rendered — no browser in this environment.
---

# Task: A Docs section in the app — quick start, and the concepts that keep confusing people

The app has no user documentation. `README.md` targets someone who has not deployed it;
`DESIGN.md` is architecture for people changing the code. Nothing serves the person who has it
running and does not know why nothing is downloading.

The user asked for docs in the nav, a quick-start covering initial setup, and — in a companion
task — per-field help popups.

## The hard requirement: describe what exists, not what you assume

**A huge amount changed on 2026-08-12/13** — roughly 30 commits, tests 489 → 887, migrations
through 016. Several features were built, shipped, and then *reversed or reshaped* the same
day. Documentation written from memory or from older docs will be wrong.

**Verify every factual claim against the code before writing it.** Where you cannot confirm
something, leave it out rather than guessing. A confidently wrong manual is worse than a short
one — and this project has already been burned by exactly that: `docker/Dockerfile`'s comment
claimed rar support for nine phases while the image had no RAR decoder at all.

## Where docs live, and the split with README

- **In-app docs are the deliverable.** They serve someone with a running instance, and they can
  link directly to the settings pages being described — something a README cannot do.
- **`README.md` keeps serving people who have not deployed yet** — what it is, how to run it,
  the volume/PUID basics — and should link onward to the in-app docs rather than duplicating
  them. Duplicated prose drifts; this repo has three separate instances of that already.

**No new frontend dependency.** Do not add a markdown renderer. Write the pages as components.
This project has added exactly one runtime frontend dependency since phase 1 and flagged it as
a deviation.

Add **Docs** to `frontend/src/nav.ts` and route it like the other top-level pages.

## Content

### Quick start — the real first-run path

Walk the actual sequence, in order, with each step linking to the page it describes:

1. **Deploy** — compose, the volume mounts and what each is for, `PUID`/`PGID`. Get this from
   `docker-compose.yml` and `README.md`, and note the volume-table correction phase 9 made:
   `local_path` is where downloads land; `staging_path` is where a completed item is
   *relocated to* afterwards. That has been documented backwards once already.
2. **Connect to the seedbox** — Settings → Connection. Host, port, username, and the auth
   choices: password, key path, or **pasted key** (added `6359569`, encrypted at rest). Mention
   the host-key policy.
3. **Create a queue** — remote path, local path, and **`sync_mode`**, which is the most
   consequential choice on the page: `move` deletes the remote copy after a verified download.
   Say so plainly here, not only in the field help.
4. **First scan** — the scan interval, and the "Rescan now" button.
5. **Queue a transfer manually** — from the Files page.
6. **Then, optionally**: auto-queue and patterns; post-processing (verify / extract / move);
   the retention and cleanup options.

### Concepts — the things that have actually confused people

These are not invented for completeness. Every one caused real confusion during 2026-08-13's
live testing:

- **Why nothing downloaded for a minute** — the settle gate. What it is, why it exists (a
  release still being written to the seedbox reads as complete and gets half-imported), and how
  to read `Arriving · 3.4 GB` versus `Waiting 1/2 · 35s`.
- **Why an item will not re-download** — auto-queue suppression. Its three reasons
  (`user_stopped`, `retries_exhausted`, `permanent_error`, plus `deleted_local`), that a
  suppressed item shows **Re-Download** rather than Queue, and that **Reset item tracking** is
  how you make a path reusable. This confused the user three separate times; it deserves the
  clearest writing in the document.
- **Dismiss vs Clear history vs Reset item tracking.** Three superficially similar actions with
  completely different blast radii. A small table comparing what each one removes and what
  survives would earn its place.
- **The lifecycle icons** — R / L / V / E, and the load-bearing distinction: **presence icons
  (R, L) describe the world right now and can go dark; milestone icons (V, E) record what
  happened and stay lit.** Include the worked example of a completed `move` item reading R dim,
  L green, V green, E green.
- **copy vs move**, including that `move` forces verification on regardless of any other
  toggle, because it gates an irreversible delete.
- **Inherit vs override** on the four post-processing toggles (`3500b3f`).

Keep each concept short. Someone reading this is stuck, not studying.

### What not to write

- No architecture. `DESIGN.md` owns that, and duplicating it guarantees drift.
- No API reference. Not asked for, and it would rot immediately.
- Nothing aspirational. If a feature has no UI yet (retention and orphan-temp cleanup are
  API-only today), either omit it or say plainly that it is configured by API only.

## Build the `FieldHelp` component here, and use it on a few fields

A companion task will apply per-field help across the settings surface. **Establish the
component in this task** so that one has a pattern to follow rather than inventing a parallel
mechanism.

- A small info icon that reveals a short explanation. **Reuse the existing hover-card machinery
  from `f4a4205`** (`FileTree.tsx`'s portal-rendered card) rather than building a third popup
  mechanism — there are already the hover card and the inline confirm panels.
- It must be keyboard reachable and work on touch. A hover-only affordance is unusable on a
  phone; the existing card already had to solve this.
- Demonstrate it on **two or three fields only** — `sync_mode` is the obvious first, since it
  can delete data. Leave the rest for the companion task.

## Before you start

- `README.md`, `docker-compose.yml`, and the Settings pages under
  `frontend/src/pages/settings/`.
- `frontend/src/nav.ts` and how existing routes are registered.
- `prompts/open-issues.md` — the "worth reading" sections are an accurate, current summary of
  the subtle behaviour, written while it was being built.
- `frontend/src/components/FileTree.tsx`'s hover card, for the popup mechanics.
- `frontend/src/lib/resetWarning.ts` — the wording there is already user-facing prose about
  consequences; stay consistent with it.

## Working tree check

`git status --porcelain`. A frontend test-runner task may have just landed. If files you need
are dirty, list them and ask.

## Conventions to honor

- If a test runner now exists (check `package.json`), add tests for anything testable you
  introduce. If it does not, do not add one — that is a separate task.
- `docs/decisions.md`, newest at top — the in-app-versus-README split, and the no-markdown-
  renderer decision.
- `CHANGELOG.md` under `### Added`; `README.md` updated to link onward rather than duplicate.
- `npm run lint` / `npm run build` clean; `uv run pytest` unchanged (887) — this task should
  touch no backend behaviour.
- **You cannot see the UI.** You cannot judge whether the docs read well or the popup is
  legible. Say so.

## When done

1. Update frontmatter (`status`, `completed`, `result`).
2. `git mv` into `prompts/done/` (or `prompts/failed/`).
3. **Do not commit.** Report back: file list, proposed one-line `feat:` message, the page
   structure you chose, which fields you demonstrated `FieldHelp` on, **any claim you could not
   verify and therefore omitted**, lint/build results, and anything not fixed. Never
   `git add -A`, never push.
