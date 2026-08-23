---
name: 2026-08-23-category-control-and-banner-link
status: done
created: 2026-08-23
model: sonnet
completed: 2026-08-23
result: >
  Fixed both findings. #14: scoped-width fix for the crushed category chip (fixed-width <select>
  wrapper, inputClasses untouched), column headers + arrow, queue options show name-only with
  path in a title tooltip, Remove now conditional on a new isStaleCategoryRow() pure function
  (stale rows only), and the detected-categories hint reworded (not persisted -- see
  docs/decisions.md) for a saved instance re-opened without a fresh Test. #13: widened
  ClientSyncScheduler.unattributed_clients / PreflightUnattributedClientOut / api/jobs.py to carry
  client_id end to end (the prompt's own premise that it already did was wrong), the banner is now
  a real react-router Link to /settings/clients?edit=<id>, ClientsTab.tsx reads it via
  useSearchParams and opens edit mode, and a second occurrence of the same wrong breadcrumb was
  found and fixed in TransferTab.tsx. All gates green: uv run pytest 2011 passed, ruff check +
  ruff format --check clean, npm run build/lint/test clean (766 frontend tests passing).
  UNVERIFIED: the actual crushed-chip layout fix has not been seen in a real browser -- jsdom has
  no layout engine, so no test proves it.
---

# Task: Make the category control legible, and make the unattributable banner a real link

Fixes findings **#13** and **#14** in `prompts/test-findings-2026-08-23.md`. **Read both first**,
and **look at the screenshot**: `private_data/screenshots/Screenshot 2026-08-23 110515.png`. It is
the evidence for #14 and it settles a question two text-based guesses got wrong.

---

## Part 1 — the category control is illegible (#14)

**What the screenshot shows:** the row renders as a one-character sliver reading `a`, then a
full-width `<select>`, then `Remove`.

**Cause:** `inputClasses` — shared by every text input on the page — contains **`w-full`**, and it
is applied to the `<select>` inside a `flex` row. The select claims the whole row and wins against
the category chip's `flex-1 truncate`, collapsing it to a single character.

**Consequence:** the only legible control shows *queue* names, so the whole thing reads as a list
of queues with no visible category side. The user's words: *"I only see a drop down list with them
in it."* The mapping's left-hand operand is invisible.

### What to fix

- **Give the category chip a real width** and stop the select consuming the row. Do not simply drop
  `w-full` from `inputClasses` — it is shared by every other input on the page and they rely on it.
  Scope the change to this control.
- **Column headers** (`Category` / `Queue`), or an explicit per-row `→`, so the direction is
  visible rather than implied by the section title.
- **Shorten the queue options.** They render `{q.name} ({q.remote_path})` — e.g.
  `ar-tv (/home/crzykidd/downloads/complete/ar-tv)`. The queue *name* is what a user thinks in; move
  the path to a `title` tooltip or muted secondary text.
- **`Remove` only on stale rows.** You cannot remove a category the client currently reports —
  leaving it unbound is how you ignore it, which is the redesign's whole point, and removing it just
  makes the row reappear on the next Test. `computeCategoryRows` **deliberately preserves** a stored
  mapping for a category the client no longer reports (see its docstring). That is the only case
  where Remove is meaningful, and such rows should be **marked** — e.g. *"not currently reported by
  this client"* — so the difference is visible.
- **The "Test the connection above to see this client's own categories" hint shows while editing a
  saved instance** (visible in the screenshot). Detected categories live only in
  `testResults[editingId]`, i.e. in memory for the session, so re-opening a saved instance loses
  them. Either persist the last detected set alongside the instance, or make the hint explain the
  situation rather than reading as an instruction the user has already followed. **Decide and
  record which.**

---

## Part 2 — the unattributable banner (#13)

Current copy (`frontend/src/components/PreflightBox.tsx`, ~line 227):

> `ultracc rtorrent: reports 2 items, none attributable to a queue — check its category → queue
> mapping in Settings → Integrations → API Clients.`

**13a — that page does not exist.** `nav.ts` has `/settings/integrations` labelled **Integrations**
(Sonarr/Radarr) and `/settings/clients` labelled **Clients** (download clients). There is no
"Settings → Integrations → API Clients" — that is the user's *eventual* unified-page idea (spec
§8.1, explicitly deferred) leaking into shipped copy. Following it literally lands on the wrong tab.

**13b — make it a link.** The banner already carries the client's id and name, so it can deep-link
to that specific client, ideally opening it in edit mode. Naming a path the user must then navigate
by hand — when the app knows exactly which record needs editing — is the avoidable half.

**Also:** grep for the same wrong breadcrumb elsewhere (other strings, help text, README, docs). If
it was written once from an imagined page name, it may appear more than once.

---

## A note on why both of these happened

This session has now produced **four** defects caused by shipped text describing something that is
not real: a placeholder that read as a filled-in value (#11c), a stale code comment that made the
seeding bug look impossible (#12), a navigation path to a page nobody built (#13), and a control
whose visible half is the wrong operand (#14).

**Worth considering as part of this task, not as a separate cleanup:** user-facing copy that names a
navigation path could be *generated from `nav.ts`* rather than hand-written, so it cannot drift from
the real routes. Same instinct as the `TransferPhase` enum-coverage guard added for #12 — make the
wrong thing impossible rather than remembering not to do it. Implement it if it is genuinely small;
if not, say so and record it.

## Tests

- The category chip renders its full name — assert on content, and note in your report that **jsdom
  cannot verify layout**, so the visual fix needs a real browser and you have not proven it.
- Stale rows (a stored category the client no longer reports) show `Remove` and a marker; live rows
  show neither.
- Queue options contain the queue name; the path is not inline in the option text.
- The banner links to the specific client and no longer names "Integrations → API Clients".
- Existing category round-trip tests still pass unchanged — the data behaviour must not move.

## Verification gates — read `CLAUDE.md`

**NEVER background a gate** — explicit timeout of at least 600000 ms on every gate Bash call.
**Run backend gates from the REPO ROOT**; use a subshell `( cd frontend && … )` so the working
directory cannot leak.

1. `uv run pytest` · 2. `uv run ruff check .` · 3. `uv run ruff format --check .`
4. `npm run build`, `npm run lint`, `npm test`

## When done

Update frontmatter, `git mv` to `prompts/done/`, record decisions in `docs/decisions.md`, append
resolutions under findings #13 and #14.
**Do not commit or push.** Report: files, every exit code, both test counts, a proposed one-line
message, your decision on the detected-categories persistence question, whether you generated the
nav copy from `nav.ts` or deferred it, and **an explicit statement of what you could not verify
without a real browser**.
