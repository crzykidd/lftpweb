# README screenshot plan

Written 2026-08-14, revised the same day against the real fixture tree the user assembled.
**Nobody has taken these yet** — this is the shooting order and the staging notes, not a record
of work done.

A coding agent cannot take them: no browser exists in the environment agents run in. These have
to be captured by hand from a running instance.

## The one warning that can cost a re-copy

**Check the queue's `sync_mode` before starting.** On a `move` queue, lftpweb deletes the source
from the seedbox once verification passes — so there is **one take per copy**. Either set the
queue to `copy` for the session, or keep a second copy of the tree elsewhere on the seedbox.

## Prep — two minutes, saves the session

**Drop the bandwidth ceiling to ~10 MB/s** (Settings → Transfer). At a typical 48 MB/s a 23 GB
pack finishes in about eight minutes and every mid-transfer state blows past before it can be
composed. At 10 MB/s the same transfer runs ~38 minutes. Put the ceiling back afterwards.

The split that matters for a time-limited session: **transient states cannot be recreated on
demand, static ones can be taken whenever.** Front-load the transient captures.

## The fixture tree these are written against

Copied into the seedbox's staging directory, ~30 GB total, deliberately generic names:

- `Show3.S01E01.Show.Name.2160p.mkv` — 4.3 GB loose file (the `pget` path)
- `Show.1.S16E13.1080p.web.H264/` — 12 rar volumes (11 × 191 MB + 23 MB), an `.nfo`, **and an
  `.sfv`** — 2.1 GB. The sidecar is what makes verification a real hash check rather than the
  weaker hash-on-disk fallback
- `Show.2.2001.S21.1080p/` — 9 `.mkv` files, 23 GB (the multi-file pack)

`docker/test-seedbox/make_demo_tree.py` builds a smaller synthetic equivalent for the dev
instance, if the real tree isn't available.

## Shooting order

### First: copy in `Show.2…S21.1080p` (23 GB)

The longest window and the most valuable frames.

| # | Shot | When | Why this one |
|---|---|---|---|
| 1 | **Files page, settling** | ~60s after the copy stops changing, before it queues | The amber `Remote · 23 GB` chip. Distinctive, and it is the behaviour that confuses people most |
| 2 | **Files page, mid-transfer** | any time in the ~38 min | *The* hero shot: directory expanded, per-file speed and ETA on each of the 9 episodes, inline progress bars |
| 3 | **Item drawer, mid-transfer** | same window | Shows the physical path under `.downloading-…` — demonstrates the folder-prefix feature in one frame |

### Then: copy in `Show.1.S16E13` (the rar directory)

Small and quick, but the only item that produces verify and extract states.

| # | Shot | When | Why |
|---|---|---|---|
| 4 | **`VERIFYING`** | right after its transfer completes | The `.sfv` makes this a real sidecar hash check; the event text says so explicitly |
| 5 | **`EXTRACTING`** | immediately after | 12 volumes × 191 MB is a genuinely visible extraction window |
| 6 | **History page** | once done | The payoff: `verify VERIFIED`, `extract`, and — on a `move` queue — `remote_delete`. The differentiator shot |

### Whenever convenient: `Show3…2160p.mkv` (4.3 GB loose)

The single-file `pget` path, and the one shape the folder prefix deliberately skips (a loose file
is complete the instant it is renamed, so there is no partial window to protect).

### Static, any time after

7. **Settings → Queues** — inherit/override toggles visible
8. **Settings → Transfer** — with the effective-lftp-settings panel expanded, showing `-c` and
   the tuning lftpweb applies
9. **Docs → How it works** — new 2026-08-14, and the page an evaluator actually reads

**Skip the Dashboard** unless there are hours of real samples behind it. A two-point chart looks
worse than no chart.

## The one shot worth going out of the way for

**History showing an amber `remote_delete_withheld` row next to successful ones.** The real
message from 2026-08-14 — *"delete withheld — verification result was CORRUPT, not VERIFIED"* —
is the single best argument the project makes for itself. It is hard to stage deliberately
(it needs a genuinely failing verification), so if one ever appears naturally, photograph it
then rather than planning for it.

## Before publishing

- **Check what is in frame.** Remote paths still contain `/home/crzykidd/…`, visible in the item
  drawer and in History event text, and the queue name may be identifying too. Crop or rename —
  this repo is public.
- **Pick one theme and keep it.** Mixed light/dark across a set looks careless. Dark tends to
  photograph better for terminal-adjacent tools.
- **Same browser width for every shot**, wide enough that the Files columns are not crowded.
  Inconsistent widths are the most common thing that makes a README look thrown together.
- Crop to the content: no OS chrome, no browser tabs, no bookmarks bar.
- Store in `docs/images/`, reference with relative paths so they render on GitHub, keep each
  under ~300 KB, PNG for UI.

## Watch for, while shooting

These are the things nobody has ever seen rendered, so the first person to look is the one who
finds them:

- **Progress-bar label contrast at ~50% fill**, where the text straddles filled and unfilled
  background. Flagged unverified since 2026-08-13.
- **The Speed column at a narrow viewport.** Its `defaultWidth` was widened 88px → 128px to fit
  `34 MB/s · 3m` and that change has never been looked at.
- **The column resize handles.** They were moved to each column's left edge (the boundary that
  actually moves, given `Name` absorbs the slack) — reasoned from the CSS, never observed.
- **The unified reset control** (All / Pattern / Selected) and the effective-lftp-settings
  `<details>` panel, both shipped 2026-08-14 and both unviewed.

Finding layout problems here is the point, not an interruption.
