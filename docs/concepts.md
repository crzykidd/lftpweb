# Concepts

The six things that actually trip people up, and what to do about each.

```jump
Nothing downloaded for a minute|#settle
An item won't re-download|#suppression
Dismiss vs Clear vs Reset|#blast-radius
The lifecycle icons|#icons
copy vs move|#copy-move
Inherit vs override|#inherit
```

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

## Why an item will not re-download — auto-queue suppression {#suppression}

Auto-queue deliberately refuses to pick an item up again once one of four things has happened to
it. This is the single most common "why is it ignoring this" and it is almost always working as
intended.

| Reason | What caused it |
|---|---|
| `user_stopped` | You stopped the transfer — either before it started or while it was running. |
| `retries_exhausted` | The transfer failed and will not be retried again on its own. Only three error classes are ever retried at all (host unreachable, TLS, and a transient local filesystem error), so this also covers a failure lftpweb could not classify. |
| `permanent_error` | The failure was one that will recur identically: auth failed, permission denied, the remote path is gone, or the disk is full. |
| `deleted_local` | lftpweb deleted the local copy itself — a manual delete from Files, or the retention sweep. |

**Suppression only ever stops auto-queue.** A manual **Queue** click on the
[Files](/files) page is never filtered by it, and using **Retry** on a failed job from
[Transfers](/transfers) lifts it.

A suppressed row whose local copy _lftpweb itself deleted_, and whose remote copy is still
there, shows **Re-Download** instead of Queue. It is the same click — the different word is
telling you this is a release you already had, back again, and that nothing will fetch it
automatically.

> **Note:** Not every "removed" row is suppressed. If an item vanished from both sides on its
> own and lftpweb resolved it as gone, it is _not_ suppressed and shows a plain **Queue**. And
> the site-wide [Re-download items removed outside lftpweb](/settings/queues) setting governs
> only the case where _something else_ — an `*arr` importer, a script, a human — took the local
> copy away. It never applies to a copy lftpweb deleted itself.

**To make a path genuinely reusable, use Reset item tracking.** Clearing History will not do
it — see below.

## Dismiss vs Clear history vs Reset item tracking {#blast-radius}

Three actions with similar names, sitting a few pixels apart, with completely different blast
radii. This is the table to check before clicking one.

| Action | Where | What it removes | What survives |
|---|---|---|---|
| **Dismiss** | [Transfers](/transfers) | Nothing. It flags one failed or cancelled job as dismissed so it stops cluttering the Transfers list. | Everything — the job is still in History, marked dismissed. Reversible in the sense that nothing was lost. |
| **Clear history** | [History](/history) | Transfer records and audit events — one row, everything matching your current filter, or everything. No category is protected, including remote-delete audit entries. | Every item, every suppression flag, every local file. Clearing History changes nothing about what will or will not download next. |
| **Reset item tracking** | [Files](/files) | The item record itself and its whole subtree — plus its settle bookkeeping and archive-cleanup bookkeeping. Its transfer records go too, as an unavoidable consequence of the item row going. | Your local files, untouched. Audit events stay in History but lose their link back to the item. |

Put plainly: **Dismiss tidies a list. Clear history deletes records. Reset item tracking forgets
a path** — it makes lftpweb treat that path as brand new on the next scan, which is the only one
of the three that changes future behaviour. That is exactly what you want after a suppressed,
stopped, or permanently-failed item, and exactly what you do not want by accident.

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
end of post-processing: it deletes the item's remote copy. Nothing else about a `move` queue
behaves differently — not the transfer, not extraction, not relocation.

**`move` forces verification on, regardless of the site-wide setting and regardless of any
per-queue override.** Verification is the sole gate on that irreversible delete, so it is not
something a toggle elsewhere can switch off underneath you. In the queue form the Verify
checkbox shows as ticked and locked, with the reason stated on it.

If verification cannot produce evidence — no `.sfv`/`.md5` sidecar, and the whole-file-read
fallback turned off — the remote delete is **withheld and audited**, not silently skipped. You
will find it on the [History](/history) page as a warning event.

> **Warning:** A `move` queue's remote path must be a hardlink pickup directory, never your
> torrent client's live seeding data. The delete is real and there is no undo.

> **Note:** One ordering quirk worth knowing: on a `move` queue the remote copy is deleted
> before extraction runs. A failed extraction therefore happens after the remote copy is gone.
> The downloaded archives are still on disk, so it is recoverable — but you recover it locally,
> not by re-downloading.

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
