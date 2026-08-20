---
name: 2026-08-20-docs-catch-up-after-redesign
status: completed        # pending | completed | failed
created: 2026-08-20
model: sonnet            # opus = research/planning, sonnet = coding
completed: 2026-08-20
result: quick-start/how-it-works/concepts/README brought in line with the redesign; concepts.md
  gained two new confusion entries (pipeline-completion, Mark complete/failed); screenshot-plan.md
  and images/README.md rewritten with a prioritized shot list (all 6 existing images dated, one
  actively misleading; 2 new shots specced); all gates green (1530 backend/566 frontend, 0 skipped)
---

# Task: bring the user-facing docs and README in line with the redesigned UI

The Transfers redesign (phase 1 of `docs/transfers-redesign-spec.md`, plus the browser-review
follow-ups and the queue pause) changed what the app's pages are called, where things live, and
what "complete" means. **The user-facing docs still describe the old UI.** That matters more here
than in most projects, because `docs/quick-start.md`, `docs/how-it-works.md` and
`docs/concepts.md` are rendered **inside the running app** under Docs — a stale sentence there is
not a stale file on GitHub, it is wrong on-screen help.

**This is a documentation-only task.** Do not change behaviour. If you find a doc that is right
and the *code* that is wrong, report it — do not "fix" the code here.

## What actually changed (verify each against the code; do not trust this list alone)

- **Navigation**: Transfers is now the main section with **Queue** and **Files** tabs
  (`/transfers/queue`, `/transfers/files`). `/files` redirects. **History is now Events**
  (`/events`), audit log only — its jobs list is gone; `/history` redirects.
- **The Queue tab** is **one globally-ordered list**, not one section per queue. Admission is
  global and queue-agnostic — grouping by queue implied per-queue lines that never existed.
- **Two boxes**: *Active / pending* and *Complete*, each with numbered pagination and a **10/20/50
  page-size selector** remembered per browser, both defaulting to 20.
- **Reordering**: ▲▲ to top, ▲ up one, ▼ down one on queued rows. Moves are **global**.
- **Rows expand to per-file progress** — the thing the Files page used to be needed for.
- **Row badges**: a queue badge using the new **short display name** (Settings → Queues), and a
  **fast-lane** badge on sub-10 MB items explaining why they can start ahead of a lower number.
- **Filtering**: a name filter, a **Dismiss list** scoped to it, and **Dismiss** as an outcome menu
  (All / Downloaded / Failed / Stopped) in the Complete box header. **"Clear all failed" is gone**,
  folded into that menu.
- **"Complete" now means the whole pipeline finished**, not that lftp exited. A downloaded item
  awaiting a Sonarr/Radarr import stays under Active with an **"Awaiting import"** label (also
  *Verifying*, *Extracting*, *Deleting source*), and moves to Complete when the *arr confirms.
- **Manual resolve**: Mark complete / Mark failed on an in-flight row, with Undo. It is
  **classification only** — it never deletes a seedbox source and is never read as a confirmed
  import. Say so in the docs; a user must not think it advances anything.
- **Queue pause**: *Pause after current* and *Pause now*, the latter leaving items ready to resume.
  **Reordering stays available while paused** — that is the point: curate the order, then unpause.
  Auto-queue keeps queueing while paused; **Start now does not work while paused**.
- Behavioural fixes worth a mention only if they already appear in the docs: auto-queue no longer
  re-queues a release the *arr just imported.

## The screenshots are the part you cannot fix

`README.md` embeds two images and `docs/screenshots.md` is a whole gallery. **They show a UI that
no longer exists.** One is actively self-contradicting: README's second image is captioned "The
Events page showing the audit trail" but the file is `docs/images/history-audit-trail.png` — a
picture of the old History page, including the jobs list that has since been removed.

**No agent in this project can take a screenshot — but the user has committed to retaking them the
evening of 2026-08-20.** So your job is to make that as close to drag-and-drop as possible:

- Do **not** delete the images, and do **not** silently reword captions to paper over the mismatch.
- **Audit every image reference** in `README.md` and `docs/screenshots.md`. For each: still
  accurate, or now wrong — and if wrong, *what the replacement should show*, specifically enough to
  act on (which page, which tab, what state the app needs to be in, what must be visible in frame).
- **Write the prose and captions for the NEW screenshots now**, against the existing filenames
  where the same slot still makes sense, so that when the user drops the files in, nothing else
  needs editing. Where a slot no longer makes sense (the old History jobs list), say what should
  replace it and pick a sensible filename.
- **Put the shot list in `docs/screenshot-plan.md`** — it already exists for exactly this, is
  already written as a shooting order, and already notes a human has to take them. Keep its format.
  Order it so the highest-value shots come first, in case the user only gets through some.
- The two README images are the priority: they are the first thing a stranger sees. Flag in your
  report which ones are *actively misleading* versus merely dated, so the user can decide whether
  to pull any temporarily. **Do not make that call yourself.**

## Before you start

- `docs/transfers-redesign-spec.md` — the whole plan and its reasoning.
- `DESIGN.md` §9 — kept current by each stage; the most reliable description of the new UI.
- `prompts/startnewsession.md`'s "Where we are" — what landed, in order, with commits.
- `docs/README.md` — explains the split between user documentation and engineering records.
  **Respect it**: `quick-start.md`/`how-it-works.md`/`concepts.md` are user docs rendered in-app;
  `decisions.md`/`audit-*.md`/the spec are engineering records and are not user-facing.

## Working tree check

Run `git status --porcelain` and cross-reference. If a file this plan touches is dirty, list it
and ask before proceeding. This prompt file is exempt.

## What to do

### 1. `docs/quick-start.md`

It walks the real first-run sequence. Check every step still matches: where you watch a transfer
now (the Queue tab, not Files), what the nav looks like, and that **every in-app link resolves** —
these are app routes, and a wrong one is a dead link for a new user.

### 2. `docs/concepts.md`

It covers "the things that actually confuse people." Several entries are stale — e.g. **"Clear
history"** is now **Clear events**, and page names have moved. More importantly, the redesign
created **new** confusions that belong here:

- *"It says Downloaded but it's still under Active — why?"* (the pipeline-completion rule)
- *"What does Pause now do to what's already downloading?"* (resumable, not cancelled, not
  suppressed)
- *"What does Mark complete actually do?"* (classification only — it does **not** delete the
  source or tell the *arr anything)

Add what earns its place; don't pad. This file is deliberately about the confusing things, not a
feature list.

### 3. `docs/how-it-works.md`

Check for page references and the "where status comes from" framing. §1.3 has not changed — do not
rewrite the architecture, only the parts that name UI.

### 4. `README.md`

"What works today" is long and now partly wrong. Update it, and **check "Known gaps"** — some
entries name the Files page or History and may be stale, superseded, or newly true. The
dismissed-jobs gap added with stage 7 is real and should stay.

### 5. `docs/screenshots.md` + `docs/screenshot-plan.md`

Per the screenshot section above.

## Conventions to honor

- **Match the existing voice.** These docs are written in a specific register — direct, concrete,
  explaining *why* rather than listing features, willing to name limitations. Read a few pages
  before writing. Do not turn them into marketing copy or a changelog.
- **Never background a verification gate.** Even for a docs change, run the gates: a broken link
  or a bad Markdown structure can fail the docs build/tests. Foreground, explicit timeout
  (600000 ms for pytest), read each exit code. From the **repo root** (not `backend/`):
  `uv run pytest`, `uv run ruff check`, `uv run ruff format --check`. From `frontend/`:
  `npm run lint`, `npx tsc -b`, `npm test` — `MarkdownDoc` parses these files at build time, so a
  structural mistake can genuinely break the frontend build.
- Conventional-Commit prefix (`docs:`). No `Co-authored-by:` trailer.

## When done

1. Update this file's frontmatter: `status`, `completed`, `result`.
2. `git mv` this file into `prompts/done/` (plain `mv` if git refuses — it is untracked).
3. **You are a spawned agent: do NOT commit.** Prepare the tree, report the file list, the
   screenshot audit, and a proposed one-line commit message. Never `git add -A`, never push.
