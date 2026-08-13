---
name: 2026-08-12-extraction-honesty-and-gating
status: done
created: 2026-08-12
model: sonnet
completed: 2026-08-12
result: |
  All three fixes shipped in core/extract.py + core/postprocess.py, plus the settings
  round-trip (models.py, api/settings.py) the new retention toggle needed to actually
  persist. 518 tests pass (up from 490 baseline + the parallel agent's own additions),
  both ruff gates clean. failed_retention_enabled defaults off (14-day default age once
  turned on) per this project's "new capability defaults off" rule, even though the
  sweep's own containment check is solid. Root cause of the production "Cannot open the
  file as archive" failure was not asserted -- only the gating gap was fixed, as directed.
---

# Task: Make extraction tell the truth, gate it on completeness, and reap its litter

Three defects in the post-processing extract step, all found on 2026-08-12 — two by code
inspection, one by a real extraction failure on the user's production instance. They are
one prompt because they all live in `core/extract.py` + `core/postprocess.py`.

## Before you start

- Read `DESIGN.md` §6 (extraction) and §7.3 (post-processing pipeline).
- Read `core/extract.py` in full — especially `extract_item`'s docstring and
  `_staging_dirs`. The `_UNPACK_`/`_FAILED_` sibling staging is **correct and must not be
  weakened**: it is what stopped a half-extracted release from ever appearing under its
  final name where Sonarr/Radarr could import it. `prompts/startnewsession.md`'s traps
  list explains why they are siblings and not children.
- Read `core/postprocess.py.process_item` and the `_do_verify` / `_do_extract` pair.
- `prompts/startnewsession.md`'s "Traps worth knowing", particularly the post-processing
  state-precedence rule.

## Working tree check

Run `git status --porcelain`. **Another agent is working in `db.py`, `core/engine.py`,
`api/ws.py`, and the frontend in parallel with you — those files are not yours; if you see
them dirty, that is expected, leave them alone, do not report it as a blocker.** If any
file *you* need is dirty, list it and ask. This prompt file is exempt.

## Fix 1 — `EXTRACTED` is claimed for items containing no archives

`core/extract.py:189-191` returns `ExtractResult(ok=True, detail="no archives found")` for
an item with nothing to extract, and `core/postprocess.py:536-539` treats any `ok=True` as
success:

```python
if result.ok:
    UPDATE item SET state = 'EXTRACTED', extracted_at = ? ...
```

So a plain `.mkv` download on a queue with auto-extract on is stamped `EXTRACTED` with an
`extracted_at` timestamp for an extraction that never happened. The user hit this on a
real item and reported the state as wrong. `ok: bool` conflates "nothing to do" with
"succeeded".

**`_do_verify` directly above already models this correctly** — three outcomes
(`VERIFIED` / `CORRUPT` / `SKIPPED`), with `SKIPPED` explicitly avoiding a false claim.
Give extraction the same shape.

**The non-obvious part:** verify's `SKIPPED` branch can hardcode a return to
`DOWNLOADED`, but extraction's no-op branch **cannot**. If verification ran first and set
`VERIFIED`, forcing `DOWNLOADED` throws away a real result. Capture the item's state
*before* `_set_item_state(..., "EXTRACTING")` and restore exactly that on a no-op.

Better still: **check `find_archives` first and skip the step entirely** when there is
nothing to extract — no `EXTRACTING` transition, no publish, no rollback. That avoids an
`EXTRACTING` flicker on the Files page for every non-archive item, which is most of them.
Prefer this; use the capture-and-restore path only where a no-op is discovered late.

Do not stamp `extracted_at` when nothing was extracted. Still write the audit `event` — it
is accurate today ("no archives found", info level) and is the only record that the step
ran at all.

## Fix 2 — extraction runs with no completeness precondition

Real failure from the user's production instance, 2026-08-12:

```
WARNING lftpweb.core.audit: event[extract] item=33404 job=None: 1 of 1 archive(s) failed:
all.american.s08e06.1080p.web.h264-ggwp.rar: ERROR: .../all.american.s08e06.1080p.web.h264-ggwp.rar
Cannot open the file as archive -- partial output kept at .../_FAILED_All.American.S08E06...
```

"Cannot open the file as archive" on a head volume almost always means the file is
truncated or zero-length, not that it is the wrong format. **The root cause was not
confirmed** — the user was going to inspect the files and had not reported back when this
prompt was written. Do not assert a root cause you have not verified; fix the gating gap,
which is real either way.

The gap: `_reap_one` fires post-processing on job success, `process_item` checks only
`item["state"] == "DOWNLOADED"`, and

```python
verify_effective = (settings.verify_enabled and bool(queue["auto_verify"])) or sync_mode == "move"
```

so on a **`copy`-mode queue with verification off — the default** — extraction is gated on
nothing but a size rollup computed at the last scan. Add two cheap preconditions in
`core/extract.py` (so they are unit-testable without the pipeline):

1. **Volume-set completeness.** For a multi-volume rar set, every volume must be present
   and non-zero before the head is handed to 7zz. Cover **both** conventions:
   old-style `.rar` + `.r00`/`.r01`/… continuation volumes, and new-style
   `.partNN.rar`. Detect gaps in the sequence (`.r00`, `.r01`, `.r03` — missing `.r02`),
   not just "some siblings exist". A missing or zero-byte volume is a clean, named
   failure — **not** an attempt that leaves `_FAILED_` litter.
2. **Zero-length head.** Never hand a zero-byte file to 7zz; report it as its own reason.

Report these as a distinct, legible outcome (the audit message is what the History page
renders verbatim — see `docs/decisions.md`'s phase 6 entry). "Volume 3 of 15 missing" is a
diagnosis; "Cannot open the file as archive" is a symptom.

**Deliberately NOT in scope:** re-checking that the item's local bytes still match its
remote bytes at extract time. That belongs with the settle-gate work, which is a separate
task, and duplicating a weaker version here would be the wrong place for it. Note it in
your report.

## Fix 3 — `_FAILED_` directories accumulate forever

`_staging_dirs` leaves `_FAILED_<name>` in place as diagnostic evidence on failure — the
right call, keep it. But nothing ever removes them, and `core/local_scan.py` filters the
prefix out of scans, so they are **invisible in the UI while consuming disk indefinitely**.
The user now has at least one.

Give them a bounded lifetime. Suggested shape, but decide for yourself and record why:

- A retention age (default something conservative like 14 days) after which a `_FAILED_`
  directory is removed, checked on the same pass that would create one, or by a small
  periodic sweep.
- **Default the sweep ON only if you are confident it can never delete anything else.**
  The naming is `_FAILED_<item name>` at the queue's local root, so containment is
  checkable: resolve the path and assert it is a direct child of the queue's `local_path`
  and that its basename starts with the prefix, before any removal. If you are not
  confident, ship it **off** by default with a setting — this project's rule is that a new
  capability defaults off, and deletion is not where to make an exception.
- Write an `event` row for every removal.
- Ensure a `_FAILED_` directory is at least *visible* somewhere — a count in the item's
  audit trail or the drawer. Something invisible that consumes disk is worse than
  something ugly.

## Conventions to honor

- Match `core/extract.py`'s existing comment style — it explains *why* and names rejected
  alternatives.
- `docs/decisions.md`, newest at top. At minimum: why extraction got verify's three-outcome
  shape rather than a second boolean, and the `_FAILED_` retention default you chose.
- `CHANGELOG.md` under `## [Unreleased]` → `### Fixed`.
- Tests for every fix, including a **real** multi-volume rar fixture with a deliberately
  missing volume. Do not assert only on mocked results — this module already has real-`7zz`
  tests; follow that precedent.
- Both lint gates: `uvx ruff@0.8.4 check --config ruff.toml .` **and**
  `uvx ruff@0.8.4 format --config ruff.toml --check .`.
- `uv run pytest` — 490 pass today. Fake seedbox up (`docker-compose.test.yml`) so nothing
  skips; tear it down afterward and confirm with `docker ps -a`.
- **You cannot see the UI.** No browser here.

## When done

1. Update this file's frontmatter (`status`, `completed`, `result`).
2. `git mv` it into `prompts/done/` (or `prompts/failed/`).
3. Record decisions in `docs/decisions.md`.
4. **Do not commit.** Prepare the tree and report back: file list, proposed one-line `fix:`
   message, test count, lint results, the `_FAILED_` retention default you chose and why,
   and anything found but not fixed. Never `git add -A`, never push.
