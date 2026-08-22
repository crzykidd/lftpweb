# Concepts

The fifteen things that actually trip people up, and what to do about each.

```jump
Nothing is downloading at all — the queue is paused|#pause
Changing the bandwidth limit restarted my transfers|#bandwidth
It says Downloaded but it's still under Active/pending|#pipeline
What "Mark complete" / "Mark failed" actually does|#manual-outcome
Nothing downloaded for a minute|#settle
A finished item looks broken for ten minutes|#removal-grace
An item won't re-download|#suppression
Dismiss vs Clear vs Reset|#blast-radius
The lifecycle icons|#icons
copy vs move|#copy-move
Inherit vs override|#inherit
The Sonarr/Radarr icon|#arr-integration
Why is this in Preflight and not downloading?|#preflight
What's in a support bundle|#support-bundle
Why old Dashboard detail disappears but the total doesn't|#daily-rollups
```

## Nothing is downloading at all — the queue is paused {#pause}

Before chasing the settle gate or a suppression flag below, check the top of
[Transfers → Queue](/transfers/queue) for an amber **Queue paused** banner, or the header bar's
**● queue paused** badge. If it's there, that's the whole explanation: someone (possibly you,
possibly a while ago) paused the transfer queue, and it survives a container restart on purpose —
so "I don't remember pausing it" doesn't rule it out.

Pausing has two flavors, both reached from the same **Pause** control:

| Mode | What it does |
|---|---|
| **Pause after current** | Nothing new starts. Whatever is already running keeps going to completion. |
| **Pause now** | Also stops whatever is running, immediately — but leaves it **ready to resume**, not restarted. The partial bytes on disk are untouched, and unpausing picks it back up from exactly where it left off, at the front of the queue it was already holding a place in. |

**This is deliberately not the same thing as Stop.** A paused-now item never carries the
`user_stopped` suppression flag (see [auto-queue suppression](#suppression) below) and never
reads `STOPPED`. It doesn't need a Retry click to come back — unpausing alone is enough, because
pausing was never a decision about *that item*, only about *right now*.

**The Queue row shows that non-destructiveness, not just states it.** A paused-now row's chip
reads something like **`QUEUED 45%`**, with the same amber fill the running row's own chip uses —
not the blue "actively transferring" one, since nothing is moving while it sits paused. That
percentage is a snapshot, not a countdown: it doesn't tick on its own, because nothing is sampling
a paused item's bytes; it only updates the next time the row re-renders with fresh data. The same
figure shows for a **retried** item that was interrupted partway (its previous attempt failed with
bytes already on disk) — the signal is "this row already has real progress on disk," not "the
queue happens to be paused right now," so both situations read identically. A queued row with
nothing downloaded yet looks exactly as it always has — no `0%`, no fill.

**What keeps running anyway.** Pausing only ever stops new transfers from starting. It does not
stop:

- Auto-queue, or a manual **Queue** click — the queue you see keeps building while paused, so
  nothing that appears on the seedbox during a pause is missed; it's simply waiting.
- Verify, extract, notify, import, and cleanup for anything already downloaded.
- Scanning, so the Files page keeps reflecting reality.

**Reordering works while paused — this is the point, not a gap.** Use the pause to rearrange the
queue (the ▲/▼/▲▲ controls on each row) so the item you actually want next is at the top, then
unpause. **Start now** is the one control that's turned off while paused (with the reason in its
tooltip) — oversubscribing past the ceiling to force one item through would defeat the pause you
just asked for.

**Pausing for a fixed duration.** Next to the Pause control is a dropdown — *until I unpause*
(the default), or *1 / 10 / 30 / 60 minutes* — that applies to whichever of the two modes above
you pick next. Once paused with a duration set, the banner and header badge both say **when**:
"Queue paused — nothing new is being admitted (resumes at 14:32)". When that time arrives, the
queue unpauses itself automatically, on the server's own clock — no page needs to be open for it
to happen, and a restart before the deadline keeps the pause (and the deadline) intact, exactly
as an indefinite pause survives a restart. A restart *after* the deadline already passed comes
back unpaused rather than resuming a stale pause. Re-picking a duration (or picking *until I
unpause*) while already paused replaces the deadline outright — it never stacks two pauses on
top of each other. Manually clicking **Unpause** also clears any deadline that was set, same as
you'd expect.

## Changing the bandwidth limit restarted my transfers {#bandwidth}

That's the option you picked, and it's the only way it could have worked.

Under the Pause control on [Transfers → Queue](/transfers/queue) there's a **bandwidth limit**
slider. It edits the *same* site-wide limit as Settings → Transfer — there is one bandwidth
ceiling for the whole instance, not one per queue — so changing it in either place changes it
everywhere. Dragging the handle doesn't save anything; it proposes a value, and then you choose
how to apply it:

| Option | What happens |
|---|---|
| **Apply to new transfers** | The new limit is saved. Nothing running is touched — each transfer keeps the speed it started at. The next thing that starts uses the new limit. |
| **Also apply to in-progress** | The new limit is saved, **and** every running transfer is stopped and immediately restarted at the new speed. |

**Why the second one has to interrupt.** lftp is handed its speed limit when it starts and gives
us no way to change it afterwards — there is no dial to turn on a transfer that's already
running. So the only way to give a running transfer a different limit is to stop it and start it
again under the new one. That's what the confirmation is telling you before you click it, and
it names how many transfers it will interrupt.

**Nothing is lost when it does.** A restarted transfer picks up from the bytes already on disk —
it does not re-download what it already had. It keeps its place in the queue, its attempt count
doesn't advance, and it is never marked **Failed** or **Stopped**. It's the same machinery as
**Pause now**, which is deliberate.

**If the queue is paused, this button won't restart it.** With the queue paused, *Also apply to
in-progress* saves the number and does nothing else: it will not unpause you, and it will not
cancel or shorten a "pause for 30 minutes" you set. (It also won't stop anything still running
under a *Pause after current* — you asked for those to finish, so they finish.) The new limit
applies to everything that starts once the pause ends.

**Zero is not "unlimited."** A limit of 0 would leave the scheduler with no room to hand out and
it would never start anything, so the slider won't go there — and it won't go below the minimum
share floor from Settings → Transfer either, for the same reason. If you want a very high
ceiling, set a very high number.

## It says Downloaded but it's still under Active/pending — why? {#pipeline}

Because "the job finished" and "the release is done" are not the same claim, and the
[Transfers → Queue](/transfers/queue) tab's two boxes split on the second one.

The transfer itself — lftp exiting, every byte landing on disk — is necessary but not
sufficient. Verify, extract, a Sonarr/Radarr import, and (on a `move` queue) the deferred
seedbox delete all continue after the job is done, and a row stays in **Active/pending** for as
long as any of them is still working. This is deliberate and applies the same way whether or
not the queue is bound to an `*arr` — a large release's own verify/extract step takes real time
even with nothing to notify, so there's one rule for "done," not a shorter one for untracked
queues.

The row says exactly what it's waiting on, instead of one vague "in progress":

| Label | What's actually happening |
|---|---|
| **Verifying** | Checking the downloaded bytes against a `.sfv`/`.md5` sidecar, or reading the whole file as the weaker fallback. |
| **Extracting** | Unpacking the release's archives. |
| **Processing** | Some other post-processing step is running — most often the move to a Final destination. |
| **Awaiting import** | Verify/extract already succeeded and the queue is bound to a Sonarr/Radarr instance. lftpweb has told it "your files are here" and is waiting for a **confirmed** import — the *arr's own queue record finishing *and* its history agreeing, checked twice — not the first ambiguous signal. |
| **Deleting source** | A `move` queue's confirmed-import delete is running or retrying. |

Only once none of those apply does the row move to **Complete**. Every one of these waits has a
bound underneath it — a live worker's existence, a *currently enabled* `*arr` instance, an age
backstop — specifically so a row can't sit in Active/pending forever with nothing actually
working on it. If one somehow does, that's what [Mark complete / Mark
failed](#manual-outcome) below is for.

## What "Mark complete" / "Mark failed" actually does {#manual-outcome}

Every row in Active/pending whose own transfer has already finished carries a **Mark complete**
/ **Mark failed** menu, with **Undo**. It's the human override for the case the bounds described
[above](#pipeline) exist to prevent but occasionally don't in practice — a release genuinely
wedged on something that is never going to resolve on its own.

**It is a classification only, and nothing more.** Clicking it:

- Moves the row into Complete (or flags it failed), so it stops sitting in Active/pending.
- Writes an audit `event` and puts a **Marked complete** / **Marked failed** chip on the row, so
  it never quietly reads like an ordinary finish.

**It deliberately never does any of the following:**

- **Delete the seedbox source** on a `move` queue. The delete ladder still waits for a genuinely
  *confirmed* import (or nothing at all, on a queue with no `*arr` binding) — clicking this is
  not that confirmation.
- **Count as a confirmed Sonarr/Radarr import.** `arr_status` is untouched.
- **Trigger notify, cleanup, retention, or any other post-processing step.**
- **Change auto-queue's eligibility** for the item.

If the real outcome turns up later anyway — the `*arr` finally confirms the import, say — it
does **not** silently overwrite your manual call. **Undo** is the only way back to letting the
pipeline decide for itself.

## Why nothing downloaded for a minute — the settle gate {#settle}

A release still being written to your seedbox looks byte-complete the moment whichever files
arrived first are whole. Download it then and you import a third of a season, extract a
truncated archive, and — on a `move` queue — delete the remote copy of a release that was never
fully there. The settle gate is what stops that.

Before an item counts as settled, a fingerprint of its whole remote subtree — **file count,
total bytes, and newest modification time** — has to be identical across **two consecutive
scans** _and_ at least **60 seconds** of wall-clock time. Both, not either: the scan count alone
can't tell a settled item from one on a queue that simply hasn't been scanned much yet.

Two readings show up on a Files row's status chip while this is happening:

- `Remote · 3.4 GB` — the remote side is _still changing_. Nothing has been confirmed unchanged
  even once yet, so there is no honest countdown to show; the byte count is what has landed on
  the seedbox so far, and it climbing is the progress signal. The chip is amber here, which is
  what separates it from the sky-blue `Remote` chip of an item that is simply sitting on the
  seedbox not being downloaded.
- `Waiting 1/2 · 35s` — it has stopped changing and the clock is running: one of the two
  required matching scans so far, 35 of the 60 required seconds. Hover the chip for the same
  thing as a full sentence.

While an item is settling, its **Remote** icon turns amber rather than green — the remote copy
really is there, it just hasn't held still long enough to trust yet. The Local icon stays dim,
because nothing has been queued.

The gate applies in two places. It stops **auto-queue** from picking an item up, and — the half
that matters more — it stops a finished download from being treated as _complete_: the item is
held instead of marked `DOWNLOADED`, and no verification, extraction, relocation, or remote
delete runs against it. **Clicking Queue by hand overrides the first, never the second.** The
worst case of queueing a still-arriving item by hand is a wasted partial transfer that resumes
later — never a bad import or a bad delete.

> **Note:** The gate is **on by default** and lives at
> [Settings → Transfer](/settings/transfer). It is a single on/off — the two-scan and
> 60-second thresholds are fixed and not tunable per install. Turning it off sheds up to about a
> minute of latency per transfer, and is only safe if your seedbox's landing path is atomic end
> to end.

## A finished item looks broken for ten minutes — the removal grace period {#removal-grace}

A release you already downloaded — verified, extracted, whatever — has its local copy vanish:
you moved it, an `*arr` importer took it, a `move` queue relocated it after verification passed.
The remote copy is still there. lftpweb does not immediately relabel the row `REMOTE_ONLY` and
treat it as never-fetched — that would make auto-queue cheerfully re-download something removed
on purpose. Instead the row holds its **last-known-good state** (`VERIFIED`, `DOWNLOADED`,
whatever it was) for a grace period — about ten minutes — before it settles on `REMOVED_LOCAL`.
Absence has to persist across several consecutive scans, not just one, so a momentary mount
hiccup or an importer mid-copy can't trigger it early.

For most of that window nothing distinguished this from an item that just quietly finished —
the row still read `VERIFIED`, both presence icons already dark, no size, no visible sign a
decision was pending. It looked broken. It was working exactly as designed, seconds from
resolving.

The status chip now shows it directly:

- `Missing · 1m` — the grace clock is running, with roughly how long is left before this row
  becomes `REMOVED_LOCAL`. Hover the chip (or open the item drawer) for the full sentence,
  including the exact time the local copy was first noticed gone.
- `Missing` with no number — the same state, but showing a number would be a guess: the row is
  right at the edge of the window, or lftpweb currently can't trust its own reading here (see
  the note on unmounted shares below). Never a stuck `0s` or a negative countdown.

> **Note:** This does not change the lifecycle icons, and should not. The **hard drive** icon
> going dim while the **shield**/**box** icons stay green is not a bug the countdown is
> covering up — it's the presence/milestone split described under [The lifecycle
> icons](#icons) working correctly. The countdown is new information (*a decision is pending*),
> not a correction to the icons (which already tell the truth about *right now* vs. *what
> happened*).

> **Warning:** If your local root is on a share that drops out (NFS, a flaky mount), the grace
> clock deliberately does not advance while lftpweb can't trust its own reading of "missing" —
> it would rather hold the last-known-good state indefinitely than misfire. The chip's own
> countdown has no visibility into that on the Files page today, so it caps at the bare
> `Missing` label once its local arithmetic reaches zero, rather than showing a number that
> might already be lying. If a row sits at `Missing` far longer than the window suggests it
> should, suspect the mount before the countdown.

**One deliberate exception: a spent archive volume never runs this clock at all.** When
"delete archives after extract" is on, the `.rar`/`.r00`/... files under a successfully
extracted release are removed on purpose, seconds after extraction finishes — not lost, the
successful conclusion of the thing that just worked. Those rows never show `Missing`, on
either `copy` or `move` queues: they settle immediately on a greyed-out **`Extracted`** chip,
the same word as the parent release's own emerald `Extracted` chip but a duller weight, meaning
"consumed, and this is why" rather than "still present and unpacked." Hover the chip (or open
the item drawer) for the plain-language explanation and the exact removal time. Underneath,
the row's real state reads `EXCLUDED` — the same reading a `file_exclude` pattern match
produces, since both mean "not counted toward completeness, for a real reason" — but the chip
never says `Excluded` for this case, since "never meant to download" would be a lie for a file
that was fetched and unpacked before this codebase cleaned it up.

## Why an item will not re-download — auto-queue suppression {#suppression}

Auto-queue deliberately refuses to pick an item up again once one of five things has happened to
it. This is the single most common "why is it ignoring this" and it is almost always working as
intended.

| Reason | What caused it |
|---|---|
| `user_stopped` | You stopped the transfer — either before it started or while it was running. |
| `retries_exhausted` | The transfer failed and will not be retried again on its own. Only three error classes are ever retried at all (host unreachable, TLS, and a transient local filesystem error), so this also covers a failure lftpweb could not classify. |
| `permanent_error` | The failure was one that will recur identically: auth failed, permission denied, the remote path is gone, or the disk is full. |
| `deleted_local` | lftpweb deleted the local copy itself — a manual delete from Files (Delete local copy), or the retention sweep. |
| `deleted_source` | You manually deleted the seedbox copy from the Files page's delete dialog (Delete source), without also deleting the local copy — so a release that later reappears under the same path is not silently fetched right back. A combined delete (both boxes checked) is recorded as `deleted_local` instead, the more complete fact about a row whose local copy is also gone. |

**Suppression only ever stops auto-queue.** A manual **Queue** click on the
[Files](/transfers/files) page is never filtered by it, and using **Retry** on a failed job from
[Transfers](/transfers/queue) lifts it.

A suppressed row whose local copy _lftpweb itself deleted_, and whose remote copy is still
there, shows **Re-Download** instead of Queue. It is the same click — the different word is
telling you this is a release you already had, back again, and that nothing will fetch it
automatically.

> **Note:** Not every "removed" row is suppressed. If an item vanished from both sides on its
> own and lftpweb resolved it as gone, it is _not_ suppressed and shows a plain **Queue**. And
> the site-wide [Re-download items removed outside lftpweb](/settings/queues) setting governs
> only the case where _something else_ — an `*arr` importer, a script, a human — took the local
> copy away. It never applies to a copy lftpweb deleted itself.

**To make a path genuinely reusable, use Reset item tracking.** Clearing events will not do
it — see below.

## Dismiss vs Clear events vs Reset item tracking {#blast-radius}

Three actions with similar names, sitting a few pixels apart, with completely different blast
radii. This is the table to check before clicking one.

| Action | Where | What it removes | What survives |
|---|---|---|---|
| **Dismiss** | [Transfers](/transfers/queue) | Nothing. It flags one failed or cancelled job as dismissed so it stops cluttering the Transfers list. | Everything — the job row itself is untouched, just marked dismissed. It no longer appears on any list page, but it's still reachable from the item drawer's own recent-history panel (open the item and look). Reversible in the sense that nothing was lost. |
| **Clear events** | [Events](/events) | Audit-event records — one row, everything matching your current filter, or everything. No category is protected, including remote-delete audit entries. | Every item, every suppression flag, every local file, and every transfer's own job record (unaffected — Events only ever holds the `event` table). Clearing events changes nothing about what will or will not download next. |
| **Reset item tracking** | [Files](/transfers/files) | The item record itself and its whole subtree — plus its settle bookkeeping and archive-cleanup bookkeeping. Its transfer records go too, as an unavoidable consequence of the item row going. | Your local files, untouched. Audit events stay in Events but lose their link back to the item. |

Put plainly: **Dismiss tidies a list. Clear events deletes audit records. Reset item tracking
forgets a path** — it makes lftpweb treat that path as brand new on the next scan, which is the
only one of the three that changes future behaviour. That is exactly what you want after a
suppressed, stopped, or permanently-failed item, and exactly what you do not want by accident.

> **Warning:** Resetting a path whose remote copy still exists, on a queue with auto-queue on,
> will start it downloading again on the next scan. Every reset panel computes and states the
> real numbers before you confirm — how many of the targets still exist remotely, whether
> auto-queue is on, and how soon the next scan is — rather than a generic warning. Read that
> line; it is accurate.

Reset lives in one control on the Files page, below the file tree, with a scope selector — the
rows you have selected, a whole queue, or a filename pattern — and Cancel always available.
Every scope follows the same flow: choose a scope, review a preview of exactly what would be
reset, then confirm. The whole-queue scope, the most destructive, additionally asks you to type
the queue's name once you have reviewed that preview. Any target that is busy —
mid-transfer, mid-post-processing, mid-delete — is skipped and reported rather than raced.

**The whole-queue and pattern previews can include items you can't currently see on the Files
page.** A row that finished, then vanished from both the seedbox and your local disk, eventually
drops off the Files tree entirely — but the database keeps tracking it, and it is exactly the
kind of stale row Reset exists to forget. Both previews say how many of the listed items are in
that state. The selected-rows scope can never show this, because it can only ever offer rows you
had in front of you to check in the first place.

## The lifecycle icons {#icons}

Every Files row carries four small icons: a **cloud** (Remote), a **hard drive** (Local), a
**shield** (Verified), and a **box** (Extracted). Hover any of them for the specific fact
behind it — sizes and timestamps, not just the name.

Colour means: **green** done and good, **amber** in progress, **red** failed, **dim** not
applicable or deliberately gone. Dim is never a fault.

The distinction that makes the whole row readable: **the two presence icons describe the world
right now and can go dark again; the two milestone icons record something that happened and
stay lit.** A file that exists locally today may not tomorrow, so the hard drive can go dim. A
file that was verified was verified — that shield does not un-light because the file later
moved.

The worked example, because it looks alarming and is not:

> **Note:** A completed item on a `move` queue that verified and extracted reads **cloud dim,
> drive green, shield green, box green**. The dim cloud is not an error — it is the queue doing
> its job. The remote copy was deleted _because_ verification passed, and hovering the cloud
> says exactly that, with the time it happened.

## copy vs move {#copy-move}

`copy` downloads and never touches the seedbox. `move` does one extra thing, once, at the very
end of post-processing — after verify *and* extract have both already run — it deletes the
item's remote copy. Nothing else about a `move` queue behaves differently — not the transfer,
not extraction, not relocation.

**`move` forces verification on, regardless of the site-wide setting and regardless of any
per-queue override.** Verification is one of the gates on that irreversible delete, so it is not
something a toggle elsewhere can switch off underneath you. In the queue form the Verify
checkbox shows as ticked and locked, with the reason stated on it.

The delete only fires once every applicable check has passed, in order — this is the "delete
ladder":

1. **Verify.** A checksum mismatch (`CORRUPT`) withholds the delete outright, always, and is
   audited on [Events](/events) as a warning event. If verification simply has no evidence to
   go on — no `.sfv`/`.md5` sidecar, and the whole-file-read fallback turned off — the delete
   **proceeds anyway** on the completeness checks the item already cleared; the Events entry
   says so plainly rather than reading like a checksum-backed delete.
2. **Extract.** If the release had archives and extraction is enabled, extraction must have
   succeeded. A failed extraction *defers* the delete instead — you'll see a "source retained"
   event, and the seedbox copy stays put until you fix the archive set and let the item's
   pipeline re-run, or delete it by hand.
3. ***arr import***, only if [Sonarr/Radarr integration](#arr-integration) has already matched
   this item. The delete then waits for the *arr to confirm it actually imported the release —
   never sooner, and never at all if the *arr's queue record disappears without an import
   (`gone`). An item on a bound queue the *arr never matched isn't held up by this at all.

There is no timeout on any of this: a withheld or deferred item keeps its seedbox copy until you
act.

> **Warning:** A `move` queue's remote path must be a hardlink pickup directory, never your
> torrent client's live seeding data. The delete is real and there is no undo.

## Inherit vs override on the post-processing toggles {#inherit}

There are four post-processing steps — **verify**, **extract**, **delete archives after
extract**, and **move to final destination** — and each one exists at two levels.

[Settings → Post-processing](/settings/post-processing) holds the **site-wide default**. Each
queue's copy of the toggle, in [Settings → Queues](/settings/queues), is by default set to
**inherit** that value — shown ticked or unticked to match, but locked, with a line saying what
it currently resolves to. Change the site-wide value and every inheriting queue follows
immediately.

**Override for this queue** unlocks it. The override is seeded at whatever the value resolves
to right now, so clicking it never changes what actually runs — it only stops the queue from
tracking the site setting. **Revert to inherit** puts it back, and tells you what it will
resolve to before you click, so you are not reverting to an invisible value.

Two toggles are conditional, and say so in place:

- **Delete archives after extract** is unavailable unless extraction actually runs for that
  queue.
- **Move to final destination** is unavailable until the queue has a Final destination set.

> **Note:** Everything post-processing does defaults to off at both levels — a fresh install
> runs none of it. The one exception in the other direction is `move` mode's forced
> verification, above.

## What the Sonarr/Radarr icon on a Files row means {#arr-integration}

If a queue is bound to a Sonarr or Radarr instance (**Settings → Integrations**, then the *arr
instance dropdown on that queue in **Settings → Queues**), a matching release gets a small mark
on its Files row once lftpweb sees it in that instance's own download queue. It stays off, with
no icon anywhere, until both an instance exists and enabled and a queue is bound to it —
three separate, deliberate acts.

The mark itself changes as the release moves through the *arr's own pipeline:

- **Plain mark** — being watched. Detected in the *arr's queue, possibly already told to
  import, but not confirmed finished yet.
- **Mark with a green ✓** — the *arr has confirmed the release fully imported. If this queue's
  "Delete when imported" is off, the files stay right where they are.
- **Mark with an amber pending dot** — the release just dropped out of the *arr's queue with no
  import evidence yet. Not necessarily a problem: download clients occasionally return a blank
  queue for a poll or two, so lftpweb rechecks every pass rather than giving up right away — if
  the release reappears in the *arr's queue it goes straight back to "being watched," and if an
  import shows up in the *arr's history it goes green. Only if **neither** happens within a
  fixed **6-hour** grace window does it escalate to the red state below. (This 6-hour window is
  a deliberate, fixed constant today, not a setting.)
- **Mark with a red ⚠** — the release left the *arr's queue and stayed unconfirmed for the full
  grace window above. Usually means the grab failed or was removed by hand on the *arr's side.
  Nothing was deleted — this state is purely informational — but it is usually worth a look, and
  it has its own entry in the Files page's filter dropdown for exactly that reason. (A release
  that reaches this red state and is *later* imported anyway — the *arr got to it eventually —
  self-heals automatically: lftpweb keeps rechecking a bounded number of times in the
  background and promotes it to green the moment an import shows up.)
- **The removal-grace countdown, reworded** — if "Delete when imported" is on for this queue,
  lftpweb removes the local copy once import is fully confirmed (never before, and never on an
  ambiguous signal). That row then runs through the exact same ~ten-minute [removal grace
  period](#removal-grace) any other locally-deleted item does — except the countdown chip reads
  **"Processed · Xm"** instead of "Missing · Xm", because this absence was deliberate and
  audited, not an alarm.

Hover the mark for which instance matched it and when.

> **Note:** "Imported" is checked carefully on purpose. A large multi-file release imports one
> file at a time on the *arr's side, so a single import event is not proof the whole release is
> done — lftpweb waits for the *arr's own queue record for the release to disappear *and* for
> history to confirm an import, and checks both are still true a minute later before treating
> anything as finished. A release simply vanishing from the *arr's queue with no import evidence
> is never treated as imported — that is exactly the amber-pending case above, and only turns red
> once it has genuinely sat unconfirmed for the full 6-hour window.

## Why is this in Preflight and not downloading? {#preflight}

The Transfers → Queue tab's small **Preflight** box, at the very top, is for one thing: something
lftpweb already knows about that it genuinely has no work to do on yet. There are two different
reasons an item can land here, and the box shows both:

**It's on a bound *arr instance's own download queue, but hasn't reached the seedbox yet.** If
Sonarr or Radarr already shows a release grabbed and downloading, but nothing has appeared in the
seedbox folder lftpweb watches, it shows up here instead of on the real Transfers list. A row
here is not stuck, not an error, and needs no action — it just hasn't arrived yet.

**It's already on the seedbox, but still being confirmed stable (the "settle gate").** A seedbox
can still be writing a multi-file release one file at a time when lftpweb's scan first sees it —
if it started downloading right then, it could grab an incomplete copy. The settle gate holds a
matched release back until its remote fingerprint (file count, total bytes, newest file's
timestamp) has held still for two consecutive scans, at least 60 seconds apart. While it's held,
you'll see it here with its known **remote size** (`remote — 22 GB`) rather than a percentage —
the whole release is already present on the seedbox, lftpweb just hasn't finished confirming
nothing is still landing. This row only ever appears for something that would otherwise be
auto-queued right now (it matches a pattern, the queue has auto-queue on, nothing else is holding
it back) — a suppressed item, or a `REMOTE_ONLY` item nothing has asked for, never shows up here
even while it's technically unsettled, since nothing is actually waiting to fetch it.

A few things about both kinds of row worth knowing:

- **It only ever shows what's still on its way.** The moment lftpweb actually sees the release
  (it lands in the watched folder and becomes a real item), or the settle gate releases a matched
  item and it gets a real transfer, the Preflight row disappears and the real item/transfer takes
  its place — never both at once. If an *arr row and a settle row both describe the same release
  (only possible if the two don't quite agree on its name), the settle row wins — it's actual
  bytes on the seedbox, not just a queue entry.
- **A release that isn't coming to this queue at all never appears.** An *arr instance's download
  queue can include other categories, or other download clients, that have nothing to do with
  this install. Preflight only shows a release once it can tell which of your queues it belongs
  to (matching the *arr's own reported folder against each queue's configured path); anything it
  can't confidently place is left off rather than guessed at.
- **An *arr row can flicker briefly out of sight and come straight back — that's expected**, the
  same as the amber pending-dot case above: download clients occasionally report an empty queue
  for a beat, and Preflight tolerates one missed check before it would ever drop a row for real.
  The settle-gated kind doesn't need this tolerance — it's recomputed fresh from lftpweb's own
  database on every scan, so there's nothing external to flicker.
- **A mount-gated queue shows a banner instead, not a wall of rows.** If a queue's local root is
  missing, unreadable, or hasn't completed a scan yet, its *entire* auto-queue pass is skipped —
  every eligible item in it, not one at a time — so Preflight shows one line naming the queue and
  why, rather than a row per affected release. This banner can appear even when the row list below
  it is empty or the box would otherwise have nothing configured to show.
- **A "Show 5/10/20" selector controls how many rows show at once**, defaulting to 5 and
  persisted per browser — the same selector, and the same `Pager`, the two boxes below it use,
  just with a smaller default set of choices since this box is smaller by intent. With nothing
  pending, the row list just says "Nothing in preflight." — and if no source is configured at
  all, the row list doesn't show up (the mount-gate banner, if any, still does).
- **Rows here have no controls** — no Stop, no Dismiss, no reordering. There's nothing to act on
  yet; once the real transfer exists, it gets the full set of controls on the list below.

## What's in a support bundle {#support-bundle}

**Settings → Logs → "Support bundle…"** builds one downloadable zip to attach to an issue or
send manually. Each part is its own checkbox, all default on:

| Part | Contents |
|---|---|
| lftpweb logs | The live log file plus every rotated file, exactly what Settings → Logs already lists. Always included — this checkbox is checked and disabled. |
| Environment snapshot | Version, build, migration level, the health readout, `lftp`/Python versions, and per-queue disk usage. |
| Settings dump | Host config, queues, patterns, transfer/post-processing/backup settings, auth mode, and *arr instances — built from the same responses the Settings pages already return, so it can never carry a password, API key, or key material. An archive extract password is a secret too, so this dump carries only how many are configured (`extract_passwords_count`), never the passwords themselves. |
| Recent audit trail | The most recent 1,000 events (the Events page's own data). |
| Recent job history | The most recent 100 jobs, including their error output. |
| A Sonarr/Radarr instance's logs | One checkbox per *enabled* instance — its own log files, fetched live from that *arr, newest-last-modified-first across every rotation series, up to a per-instance size budget (~20 MB). Hidden entirely when no instance is enabled. |

The SQLite database itself is never included — it carries every encrypted secret this app
stores, and the settings dump above covers what support actually needs. If one Sonarr/Radarr
instance can't be reached while building the bundle (unreachable, a bad key, or its listing
request itself failing), that instance's directory gets a `FETCH-FAILED.txt` note instead of
failing the whole download; one *file* within an otherwise-healthy instance failing to fetch
(seen live: a custom-script log the *arr lists but serves from a different, 404ing endpoint)
gets its own narrower `<filename>.FETCH-ERROR.txt` beside the files that did fetch, rather than
marking the whole instance failed. If an instance has more log content than the per-instance
budget allows, files are kept by the *arr's own reported last-modified time, newest first,
compared across *every* log series at once (`sonarr.*`, `sonarr.debug.*`, `sonarr.trace.*`, …)
rather than within each series separately — a dormant debug/trace series whose own newest file
is stale must never outrank a live series' current file just because it sorts first
alphabetically. A file with no usable timestamp is never assumed recent; it sorts last. A
`TRUNCATED.txt` names what didn't fit, and what did, each with its own last-modified time, so
it's obvious at a glance whether the budget bought recent material. *arr log files are carried
exactly as that *arr wrote them — unredacted, since lftpweb doesn't rewrite another app's own
logs — so give one a glance before sharing it publicly. Building a bundle writes one audit event
so there is always a record of when one was made and what it contained.

## Why old Dashboard detail disappears but the total doesn't {#daily-rollups}

The Dashboard's 24h/7d/30d charts read *raw* samples taken roughly every 30 seconds — enough
detail to see a transfer's own shape, but far too much to keep for a year (30 days' worth is
already tens of thousands of rows). Those raw rows are pruned after **30 days by default**
(configurable, up to 30) — which is why picking a range further back than that shows nothing for
the older part of it.

The **total downloaded** figure at the top of the page, and the **90d**/**1y** chart ranges,
read a *second*, much smaller table instead — one row per queue per day, kept for **13 months**.
Every closed day gets rolled up into it (summed from the same raw samples) before its detailed
rows are ever pruned, so the day's total survives even once the minute-by-minute detail behind
it is gone. That is the whole trade: the *shape* of a transfer from nine months ago is gone for
good, but *how much* moved that day isn't.

A day the container was only partly running shows up distinctly — a thin marker on that day's
bar, and "partial day, ~N% covered" in the chart's accessible table — rather than looking like
either a normal quiet day or a total gap. A day with **no** marker and no bars simply never ran
at all (before this feature existed, or the app was off that whole day); a day with bars but no
marker ran the whole day.

Both the daily table's 13-month window and the "past raw retention" boundary above are UTC
calendar days — this app stores everything in UTC with no timezone handling anywhere, the same
caveat History's date filters already carry. Away from UTC, "today" and "yesterday" can be off
by a few hours.
