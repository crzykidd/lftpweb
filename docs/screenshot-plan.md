# README screenshot plan

Written 2026-08-14, at the user's request, for screenshots to add to `README.md`. **Nobody has
taken these yet** — this is the shortlist and the staging notes, not a record of work done.

A coding agent cannot take them: no browser exists in the environment agents run in. These have
to be captured by hand from a running instance.

## Why so few

Five, not fifteen. A README's screenshots are read in about four seconds by someone deciding
whether this project is worth their evening — they answer *"what is this, and does it look like
it works?"*, not *"how do I use it?"*. The in-app **Docs** section is where explanation belongs
now that it exists. Each shot below earns its place by showing something the prose cannot.

Order matters: put them in the README in this order, the first one directly under the intro
paragraph.

## The shortlist

### 1. The Files page, mid-transfer — the one that has to carry the project

**Why:** this is the whole idea in one frame. Remote and local as one tree, with live state.
If someone only looks at one image, this is it.

**State to stage:**
- One directory item actively **DOWNLOADING** with the inline progress bar partly filled and a
  real rate in the new Speed column
- At least one **DOWNLOADED** item with V and E lit, and one **REMOTE_ONLY** item — so the R/L/V/E
  lifecycle icons visibly differ between rows and read as meaningful
- Ideally one item in **settling** ("arriving"), since that is the behaviour most likely to
  confuse a new user and it is genuinely distinctive
- A directory expanded to show children, so the tree structure is obvious
- Real release names. Sanitised placeholder names look fake and undersell it

**Watch for:** progress-bar label contrast at ~50% fill, where the text straddles filled and
unfilled background. `prompts/open-issues.md` has flagged this as unverified since 2026-08-13 and
a screenshot is where it will look worst.

### 2. The item detail drawer

**Why:** it answers "does this tell me what actually happened?" — remote vs. local size and
mtime side by side, the lifecycle chronology, recent transfers and audit events. It is the
strongest evidence the app is honest about state rather than just pretty.

**State to stage:** an item with a real history — downloaded, verified, and (if a `move` queue)
its remote copy deleted, so the chronology has several distinct entries with real timestamps
rather than one line.

### 3. Settings → Queues, showing a queue configured

**Why:** the single most common pre-install question is "how much configuration is this?" One
queue with remote path, local path, sync mode, and the inherit-or-override toggles visible
answers it faster than a paragraph.

**State to stage:** a queue with `copy` mode and at least one post-processing toggle explicitly
overridden rather than all inheriting, so the three-state control is legible. If a `FieldHelp`
popover can be captured open on a field whose wrong answer costs data (sync mode, or the folder
prefix), that is worth more than a clean shot — it shows the app explains itself.

### 4. The History page with an audit trail

**Why:** it is the differentiator. Plenty of tools transfer files; this one records every
`remote_delete`, every `remote_delete_withheld`, and every verify outcome, and will tell you it
refused to delete something.

**State to stage:** must include at least one **amber `remote_delete_withheld`** row next to
successful ones. The real message from 2026-08-14 is ideal:
*"delete withheld — verification result was CORRUPT, not VERIFIED"*. That single line is the best
argument the project makes for itself.

### 5. The Dashboard

**Why:** closes the loop for anyone who wants to see throughput over time, and it is the only
screen with a chart.

**State to stage:** at least several hours of real samples so the chart has shape. An empty or
two-point chart is worse than no screenshot — skip this one entirely rather than ship a flat
line.

## Staging notes that apply to all of them

- **Take them in both light and dark**, then pick one theme and use it consistently. Mixed themes
  across five images looks careless. Dark tends to photograph better for terminal-adjacent tools.
- **Same browser width for every shot**, wide enough that the Files columns aren't crowded.
  Inconsistent widths are the most common thing that makes a README look thrown together.
- **Check what's in the frame.** The remote paths contain a real seedbox hostname and username
  (`/home/crzykidd/downloads/...`), and queue names may too. Rename or crop before publishing —
  this repo is public.
- **Crop to the content.** No OS chrome, no browser tabs, no bookmarks bar.
- Store them in `docs/images/` and reference with relative paths so they render on GitHub.
- Keep each under ~300 KB; PNG for UI.

## One honest caveat

Every screen listed here has been built, type-checked, linted, and verified at the endpoint
level, and the user has driven the app by hand — but several of the newest surfaces
(the unified reset control, the Speed column, the Transfers timing readouts, the Markdown-rendered
Docs pages) have never been *looked at* by anyone as of this writing. Taking these screenshots
will be the first real visual review of them. Expect to find layout problems, and treat that as
the point rather than an interruption.
