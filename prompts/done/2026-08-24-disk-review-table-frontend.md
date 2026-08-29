---
name: 2026-08-24-disk-review-table-frontend
status: completed        # pending | completed | failed
created: 2026-08-24
model: sonnet            # opus = research/planning, sonnet = coding
completed: 2026-08-29
result: Rebuilt Disk review as a section per download client, each a sortable/filterable torrent
  table with capability-driven columns; debris/unclaimed/excluded_content stay grouped by
  directory below. Gates green (lint/tsc/839 vitest); no backend files touched.
---

# Task: rebuild the Disk review page as per-client sortable torrent tables

The disk review page renders three piles as three plain tables with no sorting, no filtering,
and no per-torrent detail. This task turns the claimed content into a **section per download
client**, each a sortable, filterable torrent table, and makes long names survivable.

**Depends on `prompts/done/2026-08-24-disk-review-visibility-backend.md`** — that task supplies
the `torrents`, `clients` and `excluded_content` response shapes this one renders. Do not start
until it has landed; read the response models in `backend/lftpweb/models.py` as the source of
truth for field names rather than guessing from this prompt.

## Before you start

Read:

1. `frontend/src/pages/DiskReviewPage.tsx` and `frontend/src/lib/diskReview.ts` — what exists,
   and the "grouping only, never a second reconciliation" rule the lib file states.
2. `frontend/src/components/PreflightBox.tsx` around line 148 — **the chip pattern to reuse.**
   The user's ask was "a small clip like we use in downloads"; that is this:
   `max-w-[8rem] shrink-0 truncate rounded bg-zinc-100 px-1.5 py-0.5 text-[10px] font-medium
   text-zinc-500 dark:bg-zinc-800 dark:text-zinc-400` with a `title` carrying the full text.
   Reuse it rather than inventing a second chip style.
3. `DESIGN.md` §17 and `docs/download-client-framework-spec.md` §11.
4. `CLAUDE.md` — commit rules; gates in the **foreground**, from the repo root.

## Working tree check

Before making any edits, run `git status --porcelain` and cross-reference the files
this plan needs to modify. If any have uncommitted changes, list them and ask the user before
touching them. Surface unrelated dirty files once as awareness; don't block. This file is exempt.

## What to do

Files in scope: `frontend/src/pages/DiskReviewPage.tsx`, `frontend/src/lib/diskReview.ts`,
`frontend/src/lib/diskReview.test.ts`, `frontend/src/api/types.ts`, plus a new
`frontend/src/lib/diskReviewSort.ts` (+ test) if the sort/filter logic reads better separated.

### 1. Section per client

One section per entry in the response's `clients` array, header showing the client's name and
its type as a chip. **There is exactly one seedbox host in this product today**
(`core/engine.py.load_host_config` is `ORDER BY id LIMIT 1`, "single, v1"), so the client is the
only grouping axis. Do not build speculative structure for a second host — a section header is a
fine place to add one later if multi-host ever arrives.

A client that did not report this pass still gets its section, showing its failure reason instead
of a table. A client with nothing to show gets an honest empty state, not a vanished section —
the whole point of this task is that absence and emptiness must be distinguishable.

### 2. The torrent table

Columns, in this order:

| Column | Source |
|---|---|
| Name | `transfer_name` — truncated, `title` tooltip, click expands to its files |
| Label | `category` — chip, toned by `attribution` |
| Files | `file_count` |
| Size | `size_on_disk` |
| Uploaded | `uploaded_bytes` |
| Seeded | `seed_time_s` |
| Ratio | `ratio` |

The client is the section, so it is not repeated as a column.

**Columns are derived from the section's own client capabilities, never from `client_type`.**
The response carries each client's declared field capabilities; a client that declares
`ratio`/`uploaded_bytes`/`seed_time_s` as unsupported simply **does not render those columns**
in its section. This is better than a column of dashes, and it is the capability-driven branch
§17 rule 6 requires — `if client_type === 'sabnzbd'` must not appear anywhere.

Within a rendered column, a `null` value shows as `—`. **Never render `0` or `0.00` for a
missing figure.**

Two states need their own visible markers rather than a blank cell:

- `missing_on_disk` — the client claims it, nothing was found there
- `content_path: null` — the client reported no path at all, so Files and Size are genuinely
  unknown (`—`), not zero

Expanding a torrent lists its files with rel_path and size, the same shape the current
`SeedingEstateGroupRow` expansion uses.

### 3. Sorting

Every column sortable, ascending/descending, click the header. Sort state is **per section** —
sorting the rTorrent table must not reorder the SAB table.

The comparators are pure functions with their own unit tests. `null` sorts **last** in both
directions — a torrent with no ratio is not "the lowest ratio", and this codebase has an
established instinct for exactly this (`COALESCE(queue_position, 1e18)` sorts a stray NULL last,
not first; the support-bundle log sort puts missing timestamps last). Sorting must be stable, so
a re-sort on an equal key does not shuffle rows.

### 4. Label filter

A filter listing **every category in the response, including ones this instance does not monitor
for downloads** — plus an "All labels" default. A label with no rows in the current scan must
still appear in the list, so an empty result reads "nothing in ar-music right now" rather than
"ar-music doesn't exist."

Show each label's `attribution` state in the filter and on the chip, so "not monitored here" is
legible rather than implied. Wording: prefer plain language (`Not monitored here`, `Unassigned`)
over the internal state names.

Decide with care whether the filter is global or per section, and write down which you chose and
why in `docs/decisions.md`. Global is the likelier right answer — the user's ask was about
finding content across the seedbox — but it interacts with per-section sort state, so make it a
deliberate choice rather than an accident.

### 5. The other piles

Debris, unclaimed and the new `excluded_content` pile have no torrent to hang ratio or seed time
on, and a base path can have several contributing clients (spec §11.1a exists precisely because
SAB and rTorrent share the reference layout's TV completed folder). So they stay **grouped by
directory in their own sections**, below the client sections, not folded into a client.

`excluded_content` is new: render it with its size figure and which excluded path matched, so its
absence from everything else is explained rather than felt. It is **never selectable** — no
checkbox anywhere on it, same as `unclaimed`.

Debris keeps its existing checkboxes and its link-aware running total exactly as they are.
`freedBytes` is unchanged.

**Display naming:** "Debris" is a verdict; this feature is review-only. Change the *display*
strings to say what was observed — e.g. "Not claimed by any client" for debris, "Ownership
unknown" for unclaimed. **Leave the code-level names (`debris`, `unclaimed`) and every spec
reference alone** — this is a wording change on screen, not a rename through the codebase.

### 6. Layout — the trap this repo has hit twice

Two bugs in this feature were pure layout problems **invisible to every test in the repo**: a
wrapper's `overflow-hidden` silently clipping a wide table's rightmost column, and a shared
`w-full` on a `<select>` crushing its sibling chip to one character. **jsdom performs no layout
at all**, so no test you write will catch either.

A seven-column table with a filter control above it is exactly where both recur. Specifically:

- The table needs its own `overflow-x-auto` container. The existing `overflow-hidden` rounded
  wrapper is the precise shape that caused the earlier bug — do not copy it onto a wider table.
- Give the filter `<select>` an explicit width; do not put `w-full` on it next to a chip.
- Truncation must be on the cell, with `title` on the same element, and must not let the Name
  column push the numeric columns off screen.

If anything looks wrong on screen, **ask for a screenshot before guessing.** Two guesses from
reported text were both wrong last time and one image settled it immediately.

### 7. Tests

`frontend/src/lib/diskReview.test.ts` (+ any new lib test file). Cover the pure logic:

- comparators for each sortable column, including `null`-sorts-last in **both** directions
- sort stability on equal keys
- the label list including a category with zero rows in the scan
- the filter composing with sort
- the capability-driven column set: a client declaring ratio unsupported yields a column set
  without ratio, driven by the capability input and **not** by any client-type string

Do not attempt to test layout — say so in a comment rather than writing a test that appears to
cover it.

## Conventions to honor

- Match the surrounding docstring style — these files explain *why*, including which decision a
  line reverses.
- Doc updates ship in the **same commit** as the code.
- Record the decisions from step 4 and step 5's naming change in `docs/decisions.md`, newest at
  top, with rejected alternatives.
- Gates, each its own **foreground** command from the repo root, reading each exit code:
  `npm --prefix frontend run lint`, `npm --prefix frontend run typecheck`,
  `npm --prefix frontend test`. Then `uv run pytest` from the repo root if any backend file was
  touched. Never background a gate — a spawned agent receives no completion notification and
  stalls forever.

## When done

1. Update this file's frontmatter: `status`, `completed`, `result`.
2. `git mv` this file into `prompts/done/` (success) or `prompts/failed/` (failure).
3. Record non-obvious decisions in `docs/decisions.md`.
4. **Do not commit.** Prepare the working tree, then report back to the orchestrating session:
   the file list, a one-line `feat:`-prefixed commit message, and the final test counts. The
   orchestrating session surfaces the `y/n` to the user. Never `git add -A`, never push.
