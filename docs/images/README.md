# docs/images

Screenshots referenced by [`../screenshots.md`](../screenshots.md) and by the two hero shots in
the repo [`README.md`](../../README.md).

**Filenames are load-bearing** — both documents reference these exact paths, so a file dropped
here under the right name renders with no edit.

## In place (2026-08-14) — all six now need retaking, see `../screenshot-plan.md`

The Transfers redesign (2026-08-19/20 — Queue/Files tabs, History becoming Events, the
queue-pause feature) changed the app's left nav and top tab strips. Every file below still shows
the **pre-redesign** chrome (a standalone `Files` nav entry, `History` instead of `Events`, no
`Transfers` tab strip), which makes all six at least dated. `history-audit-trail.png` is worse —
it's a screenshot of the old History page's now-removed jobs list, not just its nav — see
`../screenshot-plan.md`'s priority-1 entry.

| File | Used by | Shot | Status |
|---|---|---|---|
| `files-mid-transfer.png` | README | **Hero 1** — Files page mid-transfer, per-file speed and ETA | Dated (nav chrome only) |
| `history-audit-trail.png` | README | **Hero 2** — Events page, including an amber `remote_delete_withheld` | **Actively misleading** — pictures the removed History jobs list |
| `item-drawer.png` | screenshots.md | Item detail drawer, mid-transfer, prefixed local path | Dated (nav chrome; also predates the drawer's Events deep-link) |
| `dashboard.png` | screenshots.md | Bytes per hour by queue + live transfer-speed chart | Dated (nav chrome; predates the 7d/30d range selector) |
| `settings-transfer.png` | screenshots.md | Live connection-count readout, fast lane, retry classes | Dated (nav chrome; Settings tab strip is missing Integrations) |
| `settings-post-processing.png` | screenshots.md | Site-wide verify / extract / move defaults | Dated (nav chrome; same tab-strip staleness) |

## Not taken yet

Named here so a future session knows they were planned, not forgotten. See
[`../screenshot-plan.md`](../screenshot-plan.md) for how to stage each.

**New, from the redesign — currently the highest priority of anything on this list:**
`queue-tab.png` (the Queue tab's Active/pending + Complete boxes) ·
`queue-paused.png` (the amber "Queue paused" banner, mid-reorder)

**Carried over from 2026-08-14, still not taken:**
`settling.png` (the settle gate holding an item, amber `Remote · 23 GB`) ·
`verifying.png` · `extracting.png` · `single-file.png` (a loose `pget`) ·
`settings-queues.png` (the inherit/override toggles) · `docs-how-it-works.png`

Nothing references these, so their absence renders nothing broken.

## Before adding

PNG, cropped to content, under ~300 KB each, one consistent theme and browser width across the
set. Check for `/home/…` paths and identifying queue names before committing — this repo is
public.
