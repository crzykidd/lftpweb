# README screenshot plan

Written 2026-08-14. **Revised 2026-08-20** after the Transfers redesign (Queue/Files tabs,
History becoming Events, one globally-ordered queue, the queue-pause feature) landed and made
every screenshot currently in place either dated or, in one case, actively wrong.

## ⚠ Read this first — corrections after v0.3.1 (2026-08-22)

**The body of this plan predates v0.3.1 and is wrong in two places that will cost you time.**

**1. The bandwidth prep advice is wrong for the demo tree.** The "drop the ceiling to ~10 MB/s"
note below was written for the real ~30 GB tree. `make_demo_tree.py` emits files of 60–180 MB, so
at 10 MB/s the largest finishes in **18 seconds** and no mid-transfer frame is composable. **Use
~1 MB/s**, which gives 3–4 minutes per item. Also: as of v0.3.1 you can set this from the **Queue
page slider** (a throttle *below* the Settings → Transfer ceiling) rather than editing the ceiling
and having to put it back. The slider still refuses anything under `min_share_floor_bps`.

**2. Shots #3 and #4 must be taken on a v0.3.1-or-later build.** The Queue tab gained three things
this week that the shot notes below don't mention: the **bandwidth slider**, the **Rescan now**
button, and a **redesigned pause control**. Shot #4's note still says "pause (either mode)" — that
two-entry menu no longer exists. It is now a single dropdown (*Till I unpause / 1 / 10 / 30 / 60
min*, selection acts immediately) plus a **"Pause after active"** checkbox, disabled when nothing
is running. Photograph that shape.

**Two shots worth adding, neither in the table below:**

- **Settings → Integrations** — never photographed at all, and now carries the "How often to check
  Sonarr/Radarr" card.
- **Dashboard** — the existing entry says "optionally show the 7d/30d range selector," which is
  stale. It now has a **Total downloaded** readout, a **group-by** dropdown (hour/day/week/month)
  and **90d/1y** ranges. Needs real history behind it to look like anything.

## Test data — generate it, don't source it

```
uv run python docker/test-seedbox/make_demo_tree.py
```

Writes to `private_data/seedbox-dropbox/` (bind-mounted at `/data/dropbox` on both fake seedboxes),
idempotent, ~540 MB. Names are already deliberately generic —
`Generic.Item.1.S01E01.1080p.WEB-DL.x264-DEMO` through `.4` — covering the four shapes worth
photographing: a loose `.mkv` (`pget`), a dir with one `.mkv` + `.nfo`, **genuinely extractable**
rar volumes, and a 4-file season pack.

**If you want real titles that a *arr will actually match** (researched 2026-08-22), the list that
survives someone looking closely at a public README is short:

- **Blender open movies** — *Big Buck Bunny*, *Sintel*, *Tears of Steel*, *Elephants Dream*,
  *Cosmos Laundromat*, *Spring*. CC-BY, published by the rights holder, officially torrented, real
  TMDB entries. The 4K encodes are several hundred MB–1 GB, which photographs better than the demo
  files.
- **Sita Sings the Blues** — Nina Paley dedicated it to the public domain (CC0). Full-length, real
  TMDB entry.
- **Curated archive.org PD collections** (`publicmovies212`, Prelinger) — provenance documented.
  As of 2026-01-01 US copyright has expired on everything published **1930 or earlier**.

**What does not qualify, and why it keeps looking like it does:** presence on archive.org is not a
licensing determination — community uploads are self-serve and unvetted, and the Archive runs on
DMCA safe harbour (their own framing: it shields them "for the occasional user who uploads
infringing content"). **PBS is not a route either** — it is a private non-profit, not a federal
agency, so § 105 does not apply; the copyrights sit with WGBH/WETA/WNET and the production
companies, and free streaming is a distribution choice, not a redistribution licence. Only
clip-level federal footage inside a programme (NASA, NOAA, DoD) is PD in its own right.

**The test that saves time:** who published the licence, and where can I read it? "It's available
on X" is availability standing in for permission.

**TV specifically: don't bother hunting.** Classic-TV public-domain status is murky and often
per-episode, and the confident claims online mostly don't hold up. Use the generated tree for the
Sonarr side — you are testing *arr matching and the transfer pipeline, not video content, and
generated files give exact control over sizes and timing.

## Status: every existing screenshot needs retaking

Six shots were captured 2026-08-14 — see [`images/README.md`](images/README.md) for which. All
six show the **pre-redesign left nav**: a standalone `Files` entry, `History` instead of
`Events`, no `Transfers` tab strip. That makes every one of them at least dated.

One is worse than dated. README's second hero, `history-audit-trail.png`, is captioned "The
Events page showing the audit trail" — but the image is a screenshot of the old **History**
page's two-section layout, jobs list included, and that jobs list doesn't exist anywhere in the
app anymore (its job was absorbed into the Queue tab's Complete box). The caption describes the
page correctly; the picture behind it doesn't match. **This is the one to fix first.**

Two more shots are needed that have never existed at all: the redesigned Queue tab itself, and
the new queue-pause control. Nothing currently in the repo shows either.

A coding agent cannot take any of these — no browser exists in the environment agents run in.
Every shot below has to be captured by hand from a running `:dev` instance.

## Priority order

Highest value first, in case the session runs out before the list does. Items 1–4 are the ones
worth doing even if nothing else gets done tonight.

| # | Shot | File | Why it matters |
|---|---|---|---|
| 1 | **Events page, audit trail** | `history-audit-trail.png` (keep this filename — see note below) | README Hero 2. Currently **actively misleading**, not just dated: it shows the old History page's jobs list, which no longer exists. This is the project's front page contradicting itself. |
| 2 | **Files page, mid-transfer, current nav** | `files-mid-transfer.png` | README Hero 1. The tree/rows/icons content is still accurate — only the nav chrome around it (`Files` as a standalone item, not a Transfers tab) is dated. First thing a stranger sees; second-highest priority for that reason alone. |
| 3 | **Queue tab — Active/pending + Complete boxes** | `queue-tab.png` (new) | The single biggest thing that changed since the last shoot, and there is currently no screenshot of it anywhere. Ideally catches a row reading **Awaiting import** (needs a *arr-bound queue) and the ▲/▼/▲▲ chevrons on a queued row. |
| 4 | **Queue tab, paused** | `queue-paused.png` (new) | The newest feature (2026-08-20) and the #1 entry in Concepts' confusion list. Show the amber "Queue paused" banner plus a reorder in progress, to make the "curate, then unpause" point visible in one frame. |
| 5 | Item drawer, mid-transfer | `item-drawer.png` | Gallery. Nav chrome dated; also now has an **Events** deep-link in its header (added 2026-08-20) that the current image predates — worth including if it fits in frame. |
| 6 | Settings → Transfer | `settings-transfer.png` | Gallery. Nav chrome dated; the Settings tab strip is also missing **Integrations**, added since this shot was taken. |
| 7 | Settings → Post-processing | `settings-post-processing.png` | Gallery. Same nav/tab-strip staleness as #6. |
| 8 | Dashboard | `dashboard.png` | Gallery. Nav chrome dated. Optional bonus: the 7d/30d range selector on the bytes chart shipped after this shot was taken and has never been photographed — recapture with a wider range selected if there's real history behind it. |

Everything below this table — settling, verifying, extracting, single-file, Settings → Queues,
Docs → How it works — was never taken in the first place. Their priority relative to each other
is unchanged from the original plan; they're simply all lower priority than fixing what's
already public on GitHub and wrong. Their shot notes are kept below since the mechanics
(fixture tree, copy order, timing) still apply.

### A note on `history-audit-trail.png`'s filename

The *slot* still makes sense — the Events page's audit trail is exactly what README's second
hero should show — so this plan keeps the existing filename rather than asking for a rename
alongside the new image. Drop the new screenshot in under the same name and nothing else needs
editing. If you'd rather it not say "history" going forward, renaming it to something like
`events-audit-trail.png` is a one-line change each in `README.md` and `images/README.md` — not
required, just flagged since the old name is a little confusing to anyone browsing the repo.

## The shape: two heroes, everything else in a gallery

**Two screenshots go in `README.md`. Everything else goes in
[`screenshots.md`](screenshots.md)**, linked from the README as *"More screenshots →"*.

Nine images inline is a wall someone scrolls past. Two, chosen to answer the only two questions a
stranger actually has, are read. The gallery is where the rest belong — and it is the right place
for anything that needs a sentence of explanation to land, which is clutter in a README and
perfectly fine in a gallery.

The gallery deliberately stays out of the in-app Docs nav — only `quick-start`, `how-it-works`,
and `concepts` are wired to routes, so `screenshots.md` renders on GitHub only. Screenshots of
the app are useless inside the app.

### Hero 1 — Files page, mid-transfer

Answers *"what is this, and does it work?"* Remote and local as one tree, live progress bars,
per-file speed and ETA, lifecycle icons. One frame carries the whole idea. **Take this from
`/transfers/files`** (the Files tab under Transfers) so the left nav and top tab strip show the
current shape — `Transfers` (highlighted) with `Queue · Files` tabs, `Files` active — rather than
`Files` as its own top-level item.

### Hero 2 — Events, with the audit trail

Answers *"can I trust it?"* Verify outcomes and remote deletes — ideally including an amber
**`remote_delete_withheld`** row. Plenty of tools move files; this one tells you what it refused
to do and why. That is the differentiator. **Take this from `/events`** — the page is audit-event
log only now, no jobs-list section above it the way the old History page had one.

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
demand, static ones can be taken whenever.** Front-load the transient captures — and note that
shots #3 and #4 (the Queue tab, paused and unpaused) both benefit from having more than one item
queued at once, so the fixture tree below (three items, copied one at a time) still gives enough
overlap to catch a queued-plus-downloading Queue tab if the last item is queued before the first
finishes.

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
happen simultaneously. The exception is the Queue tab shots (#3/#4): those specifically want more
than one item in the queue at once, so queue the next copy slightly before the previous one
finishes if you want a mixed active/queued frame.

1. **`Show.2.2001.S21.1080p`** (23 GB) — the long window. Hero 1 and the Queue tab shots come
   from here.
2. **`Show.1.S16E13`** (the rar directory) — the only item that produces verify and extract
   states, which is also what makes an Active/pending row read "Verifying"/"Extracting" for the
   Queue tab shot.
3. **`Show3…2160p.mkv`** (4.3 GB loose) — whenever convenient.

## Shot order

### From copy 1 — `Show.2` (23 GB)

| # | Shot | Where | When |
|---|---|---|---|
| 1 | Files page, **settling** | gallery | ~60s after the copy stops changing, before it queues — amber `Remote · 23 GB` |
| 2 | **Files page, mid-transfer** | **HERO 1** | any time in the ~38 min. Directory expanded, per-file speed and ETA on all 9 episodes. Take from `/transfers/files` |
| 3 | Item drawer, mid-transfer | gallery | same window — shows the physical path under `.downloading-…` |
| 4 | **Queue tab, Active/pending** | **new gallery shot** | same window, from `/transfers/queue` — this item still `downloading`, ideally a second item already `queued` beneath it so the ▲/▼/▲▲ chevrons and the `#N` position are visible |
| 5 | **Queue tab, paused** | **new gallery shot** | pause (either mode) partway through, then use the chevrons to reorder the queued rows — the shot should show the amber banner *and* a reorder in progress, then unpause once captured |

### From copy 2 — `Show.1` (rar directory)

| # | Shot | Where | When |
|---|---|---|---|
| 6 | `VERIFYING` | gallery | right after its transfer completes — the `.sfv` makes this a real sidecar hash check |
| 7 | `EXTRACTING` | gallery | immediately after — 12 volumes × 191 MB is a visible window |
| 8 | **Events page** | **HERO 2** | once done — `verify VERIFIED`, `extract`, and on a `move` queue `remote_delete`. Take from `/events`, not the old History layout |

### From copy 3 — the loose file

| # | Shot | Where | When |
|---|---|---|---|
| 9 | Files page, single-file transfer | gallery | the `pget` path, and the one shape the folder prefix deliberately skips |

### Static — any time after

| # | Shot | Where | Why |
|---|---|---|---|
| 10 | Settings → Queues | gallery | answers "how much configuration is this?" — inherit/override toggles visible |
| 11 | Settings → Transfer | gallery | with the effective-lftp-settings panel expanded, showing `-c` and the applied tuning. Recapture even though this exists already — nav/tab strip is dated |
| 12 | Settings → Post-processing | gallery | recapture even though this exists already — same nav/tab-strip staleness |
| 13 | Dashboard | gallery | recapture even though this exists already — nav chrome dated; optionally show the 7d/30d range selector if there's enough sample history |
| 14 | Docs → How it works | gallery | the page an evaluator actually reads |

**Skip a fresh Dashboard capture** if there are only a couple of points behind it — a two-point
chart looks worse than no chart. Reuse the existing one until there's real history, or recapture
just for the nav-chrome fix even with sparse data if the redesign consistency matters more.

### Opportunistic

**Events showing an amber `remote_delete_withheld` row.** The real message from 2026-08-14 —
*"delete withheld — verification result was CORRUPT, not VERIFIED"* — is the single best argument
the project makes for itself, and would make Hero 2 considerably stronger. It needs a genuinely
failing verification, so it cannot be staged. If one ever appears naturally, photograph it then
and swap it into Hero 2.

**A `Missing · 1m` countdown chip** (2026-08-14) is worth a gallery frame if you happen to catch
one — it demonstrates the app explaining itself rather than showing stale data.

**A Queue tab row reading "Awaiting import"** — needs a queue bound to a Sonarr/Radarr instance
with a release mid-import. Strengthens shot #4 above considerably if the timing lines up; not
worth staging from scratch if it doesn't.

## Before publishing

- **Check what is in frame.** Remote paths still contain `/home/crzykidd/…`, visible in the item
  drawer and in Events event text, and the queue name may be identifying too. Crop or rename —
  this repo is public.
- **Pick one theme and keep it.** Mixed light/dark across a set looks careless. Dark tends to
  photograph better for terminal-adjacent tools.
- **Same browser width for every shot**, wide enough that the Files columns are not crowded.
  Inconsistent widths are the most common thing that makes a README look thrown together. This
  matters more than usual this round, since old and new shots will sit side by side in the
  gallery until everything is recaptured.
- Crop to the content: no OS chrome, no browser tabs, no bookmarks bar.
- Store in `docs/images/`, reference with relative paths so they render on GitHub, keep each
  under ~300 KB, PNG for UI.

## Watch for, while shooting

These are the things nobody has ever seen rendered, so the first person to look is the one who
finds them:

- **The `Missing` chip vs the `Remote` settling chip.** Both amber, both synthetic substitutions.
  They need to read as *different*, not merely differently worded.
- **The Queue tab's two boxes at a glance.** Active/pending and Complete need to read as visually
  distinct sections, not one long list with a divider that's easy to miss — this is the first
  time anyone will have looked at them side by side in a static image rather than live.
- **The paused banner's contrast** against both the header stats bar above it and the row list
  below it, in whichever theme gets shot.

Everything else that was on this list came off it on 2026-08-14 — the user click-tested each and
found nothing wrong: the **progress-bar label contrast at ~50% fill**, the **Speed column** at
its widened 128px `defaultWidth`, the **column resize handles** on each column's left edge, the
**unified reset control's** three scopes, the **effective-lftp-settings `<details>` panel**,
**Settings → Queues**, and **Docs → How it works**. Add back to this list whatever ships next —
the point of the list is that the first person to look is the one who finds the problem.

Finding layout problems here is the point, not an interruption.
