# lftpweb

A containerized web interface for keeping a local directory in sync with a seedbox, using
**lftp** as the transfer engine over SSH/SFTP.

Browse the remote and local trees as one view, queue and supervise downloads with live
progress, auto-queue on patterns, and optionally verify, extract, and relocate finished items.

> ## Beta
>
> **Version `0.2.6`.** All 9 build phases are built, covered by backend unit and integration
> tests plus a frontend unit suite, and exercised manually through the UI against a real
> seedbox. This is a **beta** — there is no upgrade path guaranteed between beta releases,
> and the database schema may still change between them. See
> "Known gaps" below before pointing it at anything important.

## How it works

**lftp is a transfer engine, not a status API.** Every transfer is its own short-lived `lftp`
process, handed one job and left alone. Nothing asks lftp how it is going — progress is derived
from the filesystem, comparing local bytes against the remote size the last scan recorded.

That one decision is why a wedged transfer cannot lie about its progress, why every transfer
resumes from its partial bytes, and why restarting the container mid-transfer costs seconds
rather than a re-download. The obvious alternative — one long-running lftp polled with `jobs -v`
— was tried and rejected: that output is meant for humans, and it takes the whole queue with it
when the process dies.

**[How it works](docs/how-it-works.md)** covers the rest in about two minutes: how an item gets
queued, where status actually comes from, and why. It is also in the running app under
**Docs → How it works**. [`DESIGN.md`](DESIGN.md) §1.2 and §1.3 have the long version.

## Screenshots

Remote and local as one tree, with live per-file progress, speed, and ETA:

![The Files page during a multi-file transfer](docs/images/files-mid-transfer.png)

Every verify outcome, every remote delete, and every delete withheld — with the reason:

![The Events page showing the audit trail](docs/images/history-audit-trail.png)

**[More screenshots →](docs/screenshots.md)**

## What works today

- Connect to a seedbox over SSH/SFTP; browse the remote tree alongside the local one
- Named **path queues** — one remote → local mapping each, with their own settings
- Queue transfers manually, watch live progress, stop them, resume from the partial;
  multi-select with shift-range and bulk Queue/Stop/Delete that reports partial failure
  honestly ("7 of 10 queued, these 3 failed because …"), plus text/state/lifecycle-facet filters,
  on the Files page. The delete dialog offers two independent, checkbox-driven scopes — Delete
  local copy, and (2026-08-16) Delete source (seedbox), the first manual remote-delete in the
  app, for cleaning up a failed or never-imported item without SSHing into the seedbox by hand.
  Local defaults on and Source defaults on for a `move` queue (off for `copy`, with a warning if
  checked anyway — its remote path isn't guaranteed to be a hardlink pickup directory). Every
  scope is guarded (path containment, no active job, mount sentinel; a manual source delete
  refuses rather than stopping a live transfer) and confirmed before it runs — irreversible,
  unlike Queue/Stop. A local delete marks the whole subtree, and picks each row's state from
  whether a remote copy actually survives
- Files rows carry four lifecycle icons (**R**emote / **L**ocal / **V**erified / **E**xtracted —
  presence facets may go dark, milestones stay lit), an inline progress bar inside the state
  chip, when the row last changed state, and an info icon that opens a detail drawer with both
  sides' size and modified date, the lifecycle chronology, and recent transfers and audit
  events. The tree sorts by name / size / last change / percent complete, and remembers your
  sort and your expand-or-collapse choice across reloads
- Optional scheduled retention: delete local copies older than N days, off by default, with a
  dry-run preview endpoint (no Settings-page toggle yet — see "What doesn't yet")
- Bandwidth ceiling and concurrency limits with an admission-control scheduler; per-queue scan
  intervals (10s / 30s / 60s / on-demand-only), on an engine loop that scans only the queues
  actually due and cannot stack a second scan of a queue over its own overrun
- Auto-queue on select/skip/file-exclude patterns, with a live "what would this match" preview
- **A settle gate, on by default**: a release still being uploaded to the seedbox is held until
  its remote fingerprint (file count, total bytes, newest mtime) holds still across two
  consecutive scans *and* at least 60 seconds of wall clock. Without it, a directory caught
  mid-upload can read as byte-complete off whichever files arrived first — and then be
  extracted, relocated, and (on a `move` queue) deleted from the remote with files still
  missing. It costs up to about a minute per transfer; switch it off at Settings → Transfer if
  your seedbox's landing path is atomic end to end
- **Folder prefix during transfer, on by default**: a directory item downloads into a
  hidden-by-convention folder (`.downloading-<name>` by default, configurable site-wide and
  per-queue) and is renamed to its real name only once the transfer is fully complete, so an
  importer (Sonarr, Radarr, Plex, Jellyfin — anything that skips dot-prefixed folders) can never
  see a partial multi-file release mid-arrival. Single-file downloads are unaffected — there's no
  partial window for that shape. See Settings → Transfer
- Post-processing: verify (sidecar or hash-on-disk, which now also checks total bytes so it
  cannot bless a truncated file), extract (`7zz` for zip/7z/tar/gz/bz2/xz, `unrar` for rar/rar5
  — see `NOTICE`), and `move` mode's remote delete — fired only after the *last* enabled check
  passes (completeness → verify → extract → *arr import when tracked), all with an audited trail
  on the Events page. Extraction stages into `_UNPACK_` and merges into place only on full
  success, is gated on cheap filesystem preconditions first (zero-length head volume, a gap in a
  multi-volume rar set), and can optionally delete a release's spent archive volumes once they
  have extracted — off by default
- **Optional Sonarr/Radarr integration** (`docs/arr-integration-spec.md`, off at every level by
  default): bind a queue to a Sonarr or Radarr instance from Settings → Integrations and lftpweb
  closes the whole loop — it watches that instance's download queue for a matching release,
  badges the row with the real Sonarr/Radarr logo while the release moves through download,
  verify, and extract, tells the *arr "your files are here, import now" once post-processing
  succeeds, and then waits for the *arr to *fully* confirm the import (its own queue record
  finished plus import history, held for two consecutive checks — never on an ambiguous signal)
  before doing any cleanup. On a `move` queue the **seedbox source is deleted only after that
  confirmed import** — files exist on both sides until the *arr has the release, so any failure
  is inspectable on both ends — and the optional per-queue "Delete when imported" toggle then
  removes the local working copy too, leaving the row visible with a "Processed" countdown
  before it ages out. The logo chip carries the outcome everywhere (Files, Queue, Events):
  green check once imported, red mark if a release left the *arr's queue without ever importing
  (filterable on its own, since that one usually needs a look). Stragglers are cleaned up from
  the app: the delete dialog offers independent **Local** and **Source (seedbox)** scopes, so
  failed or abandoned releases can be cleared from both sides without ever SSHing in
- **Transfers is the main section, with Queue and Files tabs** (`/transfers/queue`,
  `/transfers/files` — the old standalone Files nav entry and `/files` both redirect here). The
  Queue tab is **one globally-ordered list, not one section per queue** — admission is entirely
  queue-agnostic, so grouping by queue implied per-queue lines that never existed. Two paginated
  boxes, each with its own 10/20/50 page-size selector: **Active/pending**, which holds a row
  until its *whole pipeline* finishes (verify, extract, a confirmed Sonarr/Radarr import, a
  deferred seedbox delete — not just the transfer, so a row can read "Awaiting import" long after
  lftp itself is done), and **Complete**. Rows carry a compact queue badge, a fast-lane badge
  when they qualify, **▲ up one / ▼ down one / ▲▲ to top** reordering, and expand to per-file
  progress. A name filter, a scoped "Dismiss list", and the Complete box's own "Dismiss" outcome
  menu (All/Downloaded/Failed/Stopped) round it out. A **Mark complete / Mark failed** menu (with
  Undo) is a manual, classification-only escape hatch for a row genuinely wedged on something
  that will never resolve — it never deletes a source, confirms an import, or touches auto-queue.
  **A site-wide Pause** (Pause after current / Pause now) stops new admissions without touching
  what's already running or queued; reordering keeps working while paused, since curating the
  order and then unpausing is the point. **History is now Events** (`/events`, `/history`
  redirects) — the audit-event log only, filterable and grouped by queue, with a per-item deep
  link from a Queue or Files row's item drawer
- Rotating log viewer, on-demand `VACUUM INTO` database backups (scheduled + manual), and a
  header readout for seedbox reachability and scheduler liveness (`/api/health`)
- Credentials encrypted at rest
- **Authentication is optional and defaults off** (`AUTH_MODE=none`) — turn on a single-user
  password login or trust a reverse proxy's identity header, both from Settings → Auth. See
  "Locked out?" below before you flip it on.
- **In-app user documentation**, under **Docs** in the left nav: a quick start walking the real
  first-run sequence, and a Concepts page covering the twelve things that actually confuse people
  (the queue being paused, why a row still reads "Awaiting import" instead of Complete, what
  Mark complete/Mark failed does and doesn't do, the settle gate, the removal grace period,
  auto-queue suppression, the difference between Dismiss / Clear events / Reset item tracking,
  the lifecycle icons, `copy` vs `move`, inherit-vs-override on the post-processing toggles, the
  Sonarr/Radarr icon, and what's in a support bundle). Every step links straight to the settings
  page it describes.
  Per-field help popups (`FieldHelp`) are being applied across the settings surface, starting
  with the fields whose wrong answer costs you data

## Support bundle

**Settings → Logs → "Support bundle…"** builds one downloadable zip to attach to an issue or
send manually — generating one is recorded in the audit trail like every other diagnostic
action here. It **captures**: lftpweb's own logs (already credential-redacted at write time,
`logsetup.py`), a build/environment snapshot, a sanitized settings dump, recent audit events and
job history, and — only for whichever Sonarr/Radarr instances you tick — each enabled instance's
own log files, fetched newest-first up to a per-instance size cap. It **never captures**: the
seedbox password, SSH keys, *arr API keys (a bundle carries only `has_*` booleans, the same as
every Settings response), archive extract passwords (a count, not the passwords), the SQLite
database itself, the install secret, or host-key pins. One caveat worth knowing before you
attach one anywhere public: an included *arr instance's log files are carried **exactly as that
*arr wrote them** — lftpweb doesn't rewrite another app's own logs — so give them a glance before
sharing. Full contents, one part per checkbox: **[docs/concepts.md](docs/concepts.md#support-bundle)**.

## Safety rails: when a volume drops or a remote stops answering

> Both failure modes have the same shape — *something that is still there stops being visible* —
> and in both, the naive reading is destructive. A dropped NFS mount makes every downloaded item
> look locally absent, which reads as "re-download the whole library." An *arr that answers a
> queue poll with nothing makes every tracked release look abandoned, which reads as "give up and
> strand the source on the seedbox." Neither is allowed to follow from a single bad reading.

### A local volume that drops out

**The mount sentinel.** After every scan that finds a queue's local root present, readable and
writable, lftpweb writes a marker file (`.lftpweb-mount-ok`) at that root. The marker lives on the
*share*, so it vanishes with the share. An empty directory and an unmounted share are
indistinguishable by content alone — that is the whole reason the file exists. lftpweb also never
creates the root itself, because `mkdir` on a mount point succeeds happily against whatever
filesystem is underneath it.

A queue whose root fails that check is blocked from acting at all — for the entire queue, not item
by item. That covers auto-queue (the whole pass, not just the transitions that look risky), manual
and scheduled local deletes, retention's expiry sweep and its orphaned-extraction-debris sweep,
spent-archive cleanup after extraction, the *arr "delete when imported" cleanup, and re-queueing
items a restart marked interrupted. Blanket-per-queue on purpose: a brand-new queue whose root
never mounted has no history to compare against — every item reads "remote only" on the very first
scan — so only a blanket gate stops auto-queue transferring an entire remote tree into a directory
that isn't really there.

**The removal grace period.** An item that had a complete local copy and is now locally absent does
not become "never downloaded." It keeps showing its last known state — including a failure state
like `CORRUPT`, so you don't lose the thing you need to see — and only transitions to
`REMOVED_LOCAL` once absence has persisted for about ten minutes across consecutive scans. While
the mount gate is failing, that clock never even starts. A transition made while the mount was
healthy is sticky: the gate exists to stop a bad transition beginning, not to undo a correct one.

### A seedbox or Sonarr/Radarr that stops answering

**A failed remote scan persists nothing.** The pass aborts before any write, the last-known-good
remote tree is kept in cache rather than discarded, every item keeps its state, and the failure
surfaces as a scan error in the header readout and the UI. Don't invent data, don't throw away good
data either.

**A partial remote scan is partial success, not failure.** GNU `find` exits nonzero the moment it
can't read one subdirectory, but still prints everything else — so any nonzero exit *with usable
output* is treated as a partial scan, and only an exit with no output at all is a real failure. The
settle gate holds rather than resets on a partial scan, so a transient unreadable subtree can't
restart a release's settle clock.

**An unreachable Sonarr/Radarr backs off, per instance.** One warning and one audit event, then
capped exponential backoff from 60 seconds up to 30 minutes. Nothing is committed on a failed poll,
and one dead instance never slows the others. Every *arr HTTP call carries a 10-second timeout.

**A release vanishing from the Sonarr/Radarr queue doesn't go straight to terminal.** It commits an
amber `dropped` state — "removed from the *arr's queue Xm ago — rechecking" — re-examined every pass
for six hours. The same download reappearing sends it back to `detected`; an import turning up in
history promotes it to `imported`, with the source delete and cleanup then proceeding normally; only
six hours with neither signal commits `gone`. A poller runs on its own clock, slower than an
upstream client's momentary blip, so both halves of a two-pass confirmation can land inside the same
bad window — the grace state is what makes that survivable.

**The source delete waits for a confirmed import, never an ambiguous one** — the *arr's own queue
record finished and import history, held across two consecutive checks. Files exist on both sides
until then, so any failure is inspectable from both ends.

**A deferred source delete retries rather than firing once** (five attempts, growing backoff), so a
transient SSH failure can't strand a remote copy permanently; it pauses with one clear event rather
than logging a failure every minute for as long as a seedbox stays down. Local cleanup is withheld
while a source delete is still owed, making "delete source → delete local" an enforced order rather
than a hoped-for one. Rows that were stranded before these rails existed heal themselves — a `gone`
row still owing a source delete is re-checked against import history, bounded to ten attempts with
backoff, and promoted to `imported` if the import turns up.

Every withheld action writes an audit event, so the Events page can always tell you *why* something
didn't happen.

## What doesn't yet

All 9 build phases shipped (see `prompts/startnewsession.md` for exactly what each one built
and how it was verified), but real gaps remain:

| | Notes |
|---|---|
| Propagating local deletes to the seedbox (`sync` mode) | Designed (`DESIGN.md` §7) but **not scheduled** — built only if it proves wanted. `sync`'s own primary use case ("the importer took it, clean up the source") is now served without building it: `move`-with-the-delete-ladder plus the Files page's manual delete dialog (Delete source, above) cover it — see `DESIGN.md` §7's own note. |
| Local retention (delete-local-files-older-than-N-days) has no Settings-page UI | Backend, API (including a dry-run preview), and the background scheduler all exist (2026-08-12) and default off; only the settings screen to turn it on hasn't shipped yet — same "backend first" gap as the settle gate and Settings → Transfer before it. |

See "Known gaps" below for the rest — behavioral limitations and deliberate trade-offs, as
opposed to the unbuilt UI above.

## Known gaps

Named here rather than fixed silently or left to be rediscovered — deliberate scope reductions
and known limitations, recorded in full in `docs/decisions.md` and `prompts/open-issues.md`:

- **Post-processing only runs when a job this app spawned succeeds, or when the settle gate
  releases a hold.** A file that arrives another way (a manual `cp`, a restore) is never
  verified, extracted, or — on a `move` queue — deleted from the remote, until something
  re-touches the item.
- **Encrypted-rar password retry has never been tested against a real encrypted archive.** No
  RAR compressor exists in this project's toolchain, so the fixture can't be built. The
  equivalent 7zz path *is* tested against a real encrypted zip; the rar path is assumed correct
  by analogy, which is weaker than every other claim here.
- **Events date filters are UTC calendar days.** No timezone handling exists anywhere —
  timestamps are stored UTC and rendered with `toLocaleString()`. Away from UTC, "yesterday" can
  include a few hours of today.
- **A dismissed job has no list page that shows it anymore.** Before the Events rename
  (2026-08-20), the old History page's job list showed every terminal job including dismissed
  ones; that list is gone (the Queue tab's Complete box, its replacement, filters dismissed jobs
  out, same as it always has). The job row itself is untouched — dismissal was never deletion —
  and is still reachable one item at a time, from that item's drawer, but nothing lists every
  dismissed job across the whole install anymore.
- **API keys and session tokens are hashed with SHA-256, not argon2id.** Deliberate: a key is
  256 bits of `secrets.token_urlsafe`, not a guessable secret, so argon2's slowness would add
  latency and buy nothing.
- **Login timing isn't normalized between "unknown username" and "wrong password."** Both return
  an identical body and are rate-limited identically; only wall-clock timing differs, and only
  under repeated probing.
- **`password` auth mode with no user configured is open access, not a lockout** — see "Locked
  out?" below. Deliberate, since the alternative bricks the instance on a typo, but anyone
  reaching the API while no user row exists is in.
- **An abandoned `.downloading-` directory with no tracking history can't be deleted from the
  UI.** It is visible (the scan maps it to its logical name), but "Delete local" resolves the
  physical path from the item's recorded prefix, and an orphan predating any job has none. The
  realistic case — a stopped, failed, or `CORRUPT` item — is fully deletable. A directory left by
  a wiped database is not; remove it by hand. Note dot-prefixed directories are also skipped by
  `rm -rf *`.
- **A terminal removed row has no individual reset.** Once a row lands on `REMOVED_BOTH` with
  nothing in either tree it stops being published, so the Files page has no checkbox for it.
  Reset item tracking's **All** and **Pattern** scopes both reach it; only the per-item scope
  cannot target one on its own.
- **`net:connection-limit` can't be set from the UI.** It lives only in the `host` row's
  `connection_overrides` JSON blob with no write path anywhere, so Settings → Transfer's
  "over the limit" warning can never fire on a current install.
- **The item drawer's "actual local path" panel (folder-prefix-during-transfer's physical
  location) only appears from the Files page.** `TransfersPage.tsx` opens the same drawer but
  doesn't have the owning queue's `local_path` loaded, so that one panel simply doesn't render
  there — every other section of the drawer is unaffected.
- **On a queue with no *arr binding, an external removal slower than ~10 minutes can still
  trigger one re-queue.** When something outside lftpweb takes a finished release apart — an
  importer, a script, a person — the item reads `PARTIAL` in between, and `PARTIAL` is how a
  genuinely interrupted transfer is resumed. Since 2026-08-19 that reading is held for the
  local-absence grace period (~10 minutes) when the item was previously complete and the remote
  total hasn't changed, which covers the ordinary case; when the window lapses it is published
  as `PARTIAL` after all, on purpose, so a locally damaged copy stays re-fetchable. On an
  *arr-bound queue the `arr_status` hand-off covers the rest with no time limit; on an untracked
  queue a removal that takes longer than the window is re-queued once. Stop the job, or delete
  the item, to settle it.
- **The Sonarr/Radarr integration UI has never been click-tested in a browser.** No agent that
  built it (backend, notify/cleanup, or this UI pass) can render a page — see `docs/decisions.md`
  for the standing reason every Settings page in this project carries the same caveat. The item
  drawer also doesn't surface `arr_status` yet; only the Files-row icon and its hover text do.

## Locked out?

`AUTH_MODE` defaults to `none` — a fresh pull of this image, or an existing install that has
never visited Settings → Auth, behaves exactly as if auth didn't exist. If you turn on
`password` mode and get locked out (forgot the password, or the container starts refusing you
for any other reason), there are two independent ways back in — no browser needed for either:

1. **Set `LFTPWEB_AUTH_MODE=none`** (or `password`/`proxy`, to force a specific mode) as a
   container environment variable and restart. This overrides whatever is stored in the
   database, unconditionally, until you unset it again. Fastest if you can edit your compose
   file / restart the container.
2. **Delete the local user row directly**, if you have shell/DB access but don't want to
   restart:
   ```bash
   sqlite3 /config/lftpweb.db "DELETE FROM auth_user"
   ```
   With `password` mode on and no user configured, lftpweb treats that as open access rather
   than rejecting every request forever — sign back in via Settings → Auth to create a fresh
   user once you're in. **Say this plainly: this is a deliberate fail-open, not a fail-closed.**
   Between the `DELETE` and creating a fresh user, anyone who can reach the API has full
   access, no password required — the trade this project makes is that a five-second mistake
   (or, on a shared host, someone else's access) recovering the instance beats a five-second
   `DELETE` permanently bricking it. See "Known gaps" above.

Both routes are exercised by `tests/test_auth_api.py::test_lockout_recovery_env_var_override`
and `::test_lockout_recovery_delete_user_row`, not just documented here.

## Running it

Requires Docker and a seedbox you can reach over SSH.

```bash
docker compose up -d          # production-shaped
docker compose -f docker-compose.dev.yml up --build    # local development
```

The UI is on **`8087`** (`5187` for the Vite dev server). Configure the host and your path
queues under **Settings**.

**Once it's up, the rest of the setup guide is in the app**, under **Docs → Quick start** — it
walks connecting to the seedbox, creating a queue, the first scan, and queueing a transfer, with
every step linking to the page it describes. **Docs → Concepts** explains the behaviours that
surprise people (why a transfer waited a minute before starting, why an item won't re-download,
and what the three different "clear this" actions each actually remove). That lives in the app
rather than being repeated here on purpose: duplicated prose drifts, and only the in-app copy can
link you to the setting it's talking about. The same two pages are also plain Markdown you can
read straight from this repo without deploying anything — see
[`docs/quick-start.md`](docs/quick-start.md) and [`docs/concepts.md`](docs/concepts.md)
(indexed, alongside the project's engineering records, in [`docs/README.md`](docs/README.md)).

| Volume | Holds |
|---|---|
| `/config` | SQLite database, logs, backups, encryption key |
| `/downloads` | where lftp writes and what the reconciler scans (`local_path`) — this is the download target, not a landing zone that gets relocated out of. A directory item downloads into a hidden-prefixed subfolder here first (`.downloading-<name>`, "Folder prefix during transfer" in Settings → Transfer, on by default) and is renamed to its real name only once complete — still under `/downloads` throughout, never a second volume |
| `/staging` | optional; the *destination* a `move`-mode queue's post-processing step relocates a verified, finished item **to**, once it's fully downloaded and verified in `/downloads`. Labeled "Final destination" in Settings → Queues to match this — see `docs/decisions.md`'s phase 5 entry for why this reads backwards from the field's own name (`staging_path`) |

`PUID` / `PGID` / `UMASK` are honored, which matters if your downloads live on an NFS share —
see `DESIGN.md` §11.2.

## Design

**[`DESIGN.md`](DESIGN.md)** is the architectural source of truth, in 15 numbered sections.
Worth reading §1.3 before anything else, because the whole codebase follows from it:

> **lftp is a transfer engine, not a status API.** Progress is derived from the filesystem —
> local bytes on disk versus known remote size — and each transfer is its own short-lived lftp
> process.

[SeedSync](https://github.com/nitrobass24/seedsync) is the prior art and is worth your time if
you want something that works today. lftpweb shares no code with it; §1.2 explains what was
learned from it and where this deliberately diverges.

Non-obvious decisions, with their rejected alternatives, are logged in
[`docs/decisions.md`](docs/decisions.md).

## Development

```bash
uv run pytest                                        # test suite
docker/test-seedbox/gen_key.sh                       # throwaway key, not committed
docker compose -f docker-compose.test.yml up -d      # fake seedbox to test against
```

The fake seedbox is two sshd containers seeded with an identical known-size tree — one with GNU
`findutils`, one with busybox — so both remote-scan paths get exercised for real.

## Licence

**[AGPL-3.0](LICENSE)** — if you run a modified lftpweb as a service for other people, you have
to publish your changes.

The container image also bundles third-party programs — unmodified Alpine packages (lftp,
OpenSSH, 7-Zip, su-exec, tini) plus `unrar`, built from RARLAB source in the image's builder
stage — each under its own licence. lftpweb runs them as separate processes rather than linking
against them. See [`NOTICE`](NOTICE).
