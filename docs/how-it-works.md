# How it works

A short tour of the one decision everything else follows from.

```jump
The one idea|#one-idea
How a transfer gets queued|#queued
Where status comes from|#status
Why not just ask lftp|#why
```

## The one idea {#one-idea}

**lftp is a transfer engine, not a status API.**

lftpweb never asks lftp how a transfer is going. Every transfer is its own short-lived `lftp`
process that is handed one job and left alone, and everything shown on screen is derived from
the filesystem and the database instead.

That single choice explains most of what you see: why progress keeps updating if lftp wedges,
why a stopped transfer resumes instead of restarting, and why a restart mid-transfer costs
nothing.

## How a transfer gets queued {#queued}

Four steps, in order:

1. **Scan.** Every queue polls its remote path on a timer (10s / 30s / 60s, or on-demand only —
   [Settings → Queues](/settings/queues)) and lists the whole tree in one round trip. The local
   directory is walked at the same time. While a queue has something actually happening in it, the
   local half is re-walked every ~5 seconds so the screen keeps up.
2. **Reconcile.** The two trees are compared and each item gets a state — remote only, partial,
   downloaded, and so on. This is recomputed from scratch every pass; nothing is remembered and
   assumed still true.
3. **Decide.** An item is queued either because you clicked Queue, or because auto-queue matched
   it against that queue's patterns. Either way it must clear [the settle
   gate](/docs/concepts#settle) first, so a release still being written to your seedbox is left
   alone until it stops changing.
4. **Admit.** Queued items wait for a slot. The scheduler decides how many transfers run at once
   and splits the bandwidth ceiling between them, then spawns one `lftp` process for that one
   item. Its share is fixed when it starts and is not reshaped afterwards.

A finished process is reaped, its exit code classified, and — only if the files on disk actually
check out — the item becomes DOWNLOADED and post-processing runs.

## Where status comes from {#status}

**From the disk, not from lftp.**

Progress is local bytes measured against the remote size the last scan recorded. An in-flight
file is sampled every few seconds, including each file inside a multi-file release, which is
where the live rate and ETA come from. lftp's own output is captured and kept, but it is used for
diagnosing failures — not for tracking progress.

The practical consequences:

- **A wedged or killed transfer cannot lie about its progress**, because nothing it says is
  being believed in the first place.
- **Every transfer resumes.** Partial bytes stay on disk and `lftp` continues from them, so a
  container restart mid-transfer costs seconds, not a re-download.
- **Exit code 0 is not proof of completion.** It means lftp reported no error. Completeness is
  confirmed by reading the disk — no leftover in-flight files, and the bytes actually present —
  before anything downstream is allowed to touch the result.

## Why not just ask lftp {#why}

Because it was tried, and it does not survive contact with reality.

The obvious design is one long-running `lftp` with everything queued inside it, polled with
`jobs -v` for status. That output is meant for humans: it is not stable to parse, it changes
shape between transfer types, and it disappears entirely the moment the process dies — taking the
queue with it. Worse, it is a single point of failure for every transfer at once.

Deriving state from the filesystem is more work up front and duller to read, but it has no such
failure mode. The disk is the truth, the database records what was asked for, and lftp is left to
do the one thing it is genuinely excellent at.

The full architecture, including the parts this page skips, is in
[`DESIGN.md`](https://github.com/crzykidd/lftpweb/blob/main/DESIGN.md) — §1.2 and §1.3 cover this
decision at length.
