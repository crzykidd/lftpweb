---
name: 2026-08-23-preflight-provenance-and-ui
status: done
created: 2026-08-23
model: sonnet
completed: 2026-08-23
result: >
  Re-verified §9.2's status precedence first -- it already worked (finding #2's fix alone had
  resolved the symptom); only provenance display was genuinely missing. Widened `PreflightRow`
  with a minimal `contributors` field (first change to that dataclass in six tasks) so a merged
  row shows both source badges and, on a new local expand toggle, both contributors' own raw
  detail -- information only, no controls. Disk review now groups by torrent (seeding estate) and
  by directory (debris) for display, `reconcile()` untouched and the reclaim total still
  link-aware through the rollup. Root-caused and fixed #8: not a state bug, `overflow-hidden` was
  silently clipping the Edit/Delete column once a passing test's detected-base-paths panel widened
  the row past its container -- changed to `overflow-x-auto`. All four backend gates and all three
  frontend gates green; 2004 backend tests, 751 frontend tests (both up from the session start).
---

# Task: Merged Preflight rows show both sources; Preflight and Disk review get detail

Fixes findings **#3**, **#6**, **#7** and **#8** in `prompts/test-findings-2026-08-23.md`.
**Read all four first.**

---

## Part 1 — a merged row must show both contributors (#3)

> *"We should show a sonarr AND a SAB icon and have the latest status from SAB. This is the thing I
> told you yesterday."*

**First, re-verify the status half.** The original symptom — Preflight showing the *arr's status
rather than the client's — was measured when **zero** client rows existed (no category mappings,
finding #2), so spec §9.2's precedence had nothing to prefer and the *arr row stood unopposed.
Client rows now flow. **Write a test that proves the client's status wins on a merged row, and
report whether it already passed before you changed anything.** If it did, say so plainly rather
than claiming a fix.

**The provenance half is genuinely missing regardless.** `PreflightRow` carries a single
`source`/`source_kind`/`source_label` triple (`core/preflight.py`), so a row deduped across the *arr
and a download client can only ever display one badge.

- Widen the row so a merged row can carry **both** contributors, and render both icons.
- **This is the first change to that deliberately minimal dataclass in six tasks.** Its docstring
  explains why it is thin and source-agnostic — honour that. Add provenance, not a grab-bag: no
  controls, no ids, nothing that implies an `item` or `job` behind the row.
- §9.2 specifies *precedence* but says nothing about *provenance display*. That is a genuine spec
  gap — record it in §9.2 as part of this task.

## Part 2 — Preflight rows expand for detail (#6)

> *"It is probably time to add a preflight expand option that shows more detail."*

An expand showing per-source detail: which client, which category, the client's own `raw_status`,
size and remaining, and the *arr's own view alongside.

**Preflight rows are deliberately inert** — `core/preflight.py`: *"no id, no bytes-done, no queue
position — there is no `item` and no `job` behind a row here."* An expand adds **information, never
controls**. Nothing in the expanded view may offer an action, because there is nothing to act on
yet. Keep §4.6's "framed as a cache" rule intact: everything shown is re-fetchable and truncating
it is always safe.

## Part 3 — Disk review groups by torrent, not by file (#7)

> *"Found 1 debris that looks right. The display of everything else seems right, but since it just
> shows files it is hard to map — it would be better to show Torrents and expand each torrent to
> see details like files etc."*

**The detection is working** — one candidate, and it looked correct. Only the presentation is
wrong.

- `reconcile()` must keep operating per-file — inode accounting is inherently per-file (spec
  §11.1b). This is a **display-layer rollup**, not a change to the reconciliation. Do not move
  set-math into the UI.
- **The two piles group differently, and that is not a detail.** The **seeding estate** groups
  naturally by torrent (the claiming client's `content_path`). **Debris** by definition has no
  torrent to group under, so it groups by directory. Do not force one shape onto both.
- Keep the **link-aware** reclaim total (spec §10.5) correct through the rollup: a group's total
  must still count a file's bytes only when the selection removes its last link. A naive per-group
  sum reintroduces exactly the lie that §10.5 exists to prevent.

## Part 4 — the edit button disappears after a successful test (#8)

> *"After testing a site and it passes I lose the edit button till I reload the page."*

A frontend state bug in `ClientsTab.tsx` — the test-result render path drops the row's action
affordances.

**Note it was investigated and cleared as the cause of #11b** (the vanishing category mappings —
that turned out to be the placeholder-text bug), so this is a real, independent defect and not a
duplicate. Fix it on its own terms.

While here: check whether running a **test while an edit form is open** disturbs the in-progress
draft. That was the leading hypothesis for #11b before the real cause was found; it may still be a
latent bug even though it was not the one reported.

---

## Tests

- A row reported by both the *arr and a client renders **both** source badges.
- On a merged row, the client's `status_label` wins per §9.2 — **and note whether this passed
  before your change**.
- A row from a single source still renders exactly one badge (no empty second slot).
- The Preflight expand shows per-source detail and **offers no action**.
- Disk review rolls up: seeding estate by torrent, debris by directory.
- A group's reclaim total is link-aware — selecting one of two links reports **zero** bytes.
- The edit affordance survives a successful test, and a test with an edit form open does not
  discard the draft.

## Verification gates — read `CLAUDE.md`

**NEVER background a gate** — explicit timeout of at least 600000 ms on every gate Bash call.
**Run backend gates from the REPO ROOT**; if you `cd` into `frontend/`, `cd` back.

1. `uv run pytest` · 2. `uv run ruff check .` · 3. `uv run ruff format --check .`
4. `npm run build`, `npm run lint`, `npm test` from `frontend/`

## When done

Update frontmatter, `git mv` to `prompts/done/`, record decisions in `docs/decisions.md`, record
the §9.2 provenance gap in the spec, and append resolutions under findings #3, #6, #7 and #8.
**Do not commit or push.** Report: files, every exit code, both test counts, a proposed one-line
message, **whether §9.2's precedence already worked before your change**, and anything else found.
