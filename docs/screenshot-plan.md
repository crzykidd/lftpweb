# README screenshot plan

Written 2026-08-14, revised the same day against the real fixture tree. **Nobody has taken these
yet** — this is the shooting order and the staging notes, not a record of work done.

A coding agent cannot take them: no browser exists in the environment agents run in. These have
to be captured by hand from a running instance.

## The shape: two heroes, everything else in a gallery

**Two screenshots go in `README.md`. Everything else goes in
[`screenshots.md`](screenshots.md)**, linked from the README as *"More screenshots →"*.

Nine images inline is a wall someone scrolls past. Two, chosen to answer the only two questions a
stranger actually has, are read. The gallery is where the rest belong — and it is the right place
for anything that needs a sentence of explanation to land, which is clutter in a README and
perfectly fine in a gallery.

It also de-risks the shoot: only two frames have to be *good*. The gallery can be utilitarian and
can grow later without touching the README.

The gallery deliberately stays out of the in-app Docs nav — only `quick-start`, `how-it-works`,
and `concepts` are wired to routes, so `screenshots.md` renders on GitHub only. Screenshots of
the app are useless inside the app.

### Hero 1 — Files page, mid-transfer

Answers *"what is this, and does it work?"* Remote and local as one tree, live progress bars,
per-file speed and ETA, lifecycle icons. One frame carries the whole idea.

### Hero 2 — History, with the audit trail

Answers *"can I trust it?"* Verify outcomes and remote deletes — ideally including an amber
**`remote_delete_withheld`** row. Plenty of tools move files; this one tells you what it refused
to do and why. That is the differentiator.

**Take several frames of Hero 1** at different fill levels during the long transfer and pick the
best afterwards, rather than trying to nail it live.

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

## Copy order

Copy these onto the seedbox **one at a time, in this order**, not all at once — each produces a
different set of transient states, and overlapping them means two things you want to photograph
happen simultaneously.

1. **`Show.2.2001.S21.1080p`** (23 GB) — the long window. Hero 1 comes from here.
2. **`Show.1.S16E13`** (the rar directory) — the only item that produces verify and extract
   states. Hero 2 comes from here, once it finishes.
3. **`Show3…2160p.mkv`** (4.3 GB loose) — whenever convenient.

## Shot order

### From copy 1 — `Show.2` (23 GB)

| # | Shot | Where | When |
|---|---|---|---|
| 1 | Files page, **settling** | gallery | ~60s after the copy stops changing, before it queues — amber `Remote · 23 GB` |
| 2 | **Files page, mid-transfer** | **HERO 1** | any time in the ~38 min. Directory expanded, per-file speed and ETA on all 9 episodes |
| 3 | Item drawer, mid-transfer | gallery | same window — shows the physical path under `.downloading-…` |

### From copy 2 — `Show.1` (rar directory)

| # | Shot | Where | When |
|---|---|---|---|
| 4 | `VERIFYING` | gallery | right after its transfer completes — the `.sfv` makes this a real sidecar hash check |
| 5 | `EXTRACTING` | gallery | immediately after — 12 volumes × 191 MB is a visible window |
| 6 | **History page** | **HERO 2** | once done — `verify VERIFIED`, `extract`, and on a `move` queue `remote_delete` |

### From copy 3 — the loose file

| # | Shot | Where | When |
|---|---|---|---|
| 7 | Files page, single-file transfer | gallery | the `pget` path, and the one shape the folder prefix deliberately skips |

### Static — any time after

| # | Shot | Where | Why |
|---|---|---|---|
| 8 | Settings → Queues | gallery | answers "how much configuration is this?" — inherit/override toggles visible |
| 9 | Settings → Transfer | gallery | with the effective-lftp-settings panel expanded, showing `-c` and the applied tuning |
| 10 | Docs → How it works | gallery | the page an evaluator actually reads |

**Skip the Dashboard** unless there are hours of real samples behind it. A two-point chart looks
worse than no chart.

### Opportunistic

**History showing an amber `remote_delete_withheld` row.** The real message from 2026-08-14 —
*"delete withheld — verification result was CORRUPT, not VERIFIED"* — is the single best argument
the project makes for itself, and would make Hero 2 considerably stronger. It needs a genuinely
failing verification, so it cannot be staged. If one ever appears naturally, photograph it then
and swap it into Hero 2.

**A `Missing · 1m` countdown chip** (2026-08-14) is worth a gallery frame if you happen to catch
one — it demonstrates the app explaining itself rather than showing stale data.

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
- **The `Missing` chip vs the `Remote` settling chip.** Both amber, both synthetic substitutions.
  They need to read as *different*, not merely differently worded.
- **The unified reset control** (All / Pattern / Selected) and the effective-lftp-settings
  `<details>` panel, both shipped 2026-08-14 and both unviewed.

Finding layout problems here is the point, not an interruption.
