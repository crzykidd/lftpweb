# Screenshots

The two shots that live in [`README.md`](../README.md) answer "what is this, and does it work?"
and "can I trust it?". This page is everything else — the states you actually spend time looking
at once it's running.

> **Note:** Images are not in place yet. Each section below has its caption written and a
> placeholder path; drop the file at that path and it renders. The shooting order and staging
> notes are in [`screenshot-plan.md`](screenshot-plan.md).

## Waiting for a release to stop arriving

![The settle gate holding an item](images/settling.png)

A release still being written to the seedbox is held before anything is queued. The amber
`Remote · 23 GB` chip means the remote side is still changing — the byte count climbing *is* the
progress signal, because there is nothing honest to count down to yet. Once it holds still, the
chip becomes `Waiting 1/2 · 35s` and then the transfer starts.

Without this, a directory caught mid-upload reads as byte-complete off whichever files arrived
first, and gets extracted, relocated, and — on a `move` queue — deleted from the remote with
files still missing. See [Concepts → the settle gate](concepts.md#settle).

## What a single item is actually doing

![The item detail drawer](images/item-drawer.png)

Both sides' size and modified time, the lifecycle chronology, recent transfers, and the audit
events for this item. While a transfer is in flight it also shows where the bytes physically are
— a directory downloads into `.downloading-<name>/` and only takes its real name once
post-processing has succeeded, so an importer never sees an incomplete or unverified release.

## Verification

![An item being verified](images/verifying.png)

Verification reads a `.sfv`/`.md5` sidecar when the release ships one, and falls back to reading
every byte off disk when it doesn't. On a `move` queue it is the only thing standing between a
truncated download and deleting the sole remaining copy — so it is forced on there regardless of
any other toggle.

## Extraction

![An item being extracted](images/extracting.png)

Multi-volume archives are checked for completeness first — a zero-length head volume, or a gap in
the sequence, fails cheaply before `unrar` is invoked. Extraction stages into `_UNPACK_<name>` and
merges into place only on full success; a failure leaves `_FAILED_<name>` as evidence rather than
a half-unpacked release under its real name.

## A single-file transfer

![A loose file transferring](images/single-file.png)

A loose file at the queue root takes the `pget` path. It's the one shape the in-flight folder
prefix deliberately skips — a single file is complete the instant lftp renames it off its
temporary name, so there is no partial window for an importer to catch.

## Per-queue configuration

![Settings → Queues](images/settings-queues.png)

One remote → local mapping per queue, each with its own scan interval, sync mode, auto-queue
patterns, and post-processing toggles. The toggles are inherit-or-override, not on/off: a queue
follows the site-wide default until you explicitly set it otherwise.

## What lftpweb tells lftp

![Settings → Transfer, effective lftp settings](images/settings-transfer.png)

Bandwidth ceiling, concurrency, and the tuning applied to every transfer — shown generated from
the same code that builds the real command, not a hand-maintained list that could drift. `-c`
(resume) is why a container restart mid-transfer costs seconds rather than a re-download.

## The documentation, in the app

![Docs → How it works](images/docs-how-it-works.png)

The same Markdown that lives in this repo, rendered in the running app: a quick start, how it
works, and the concepts that actually trip people up. One source — reading it here and reading it
in the app show identical text, because it is the same file.
