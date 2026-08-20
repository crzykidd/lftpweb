# Quick start

Six steps from a running container to a downloading queue. Each one links to the page it
describes.

## 1. Deploy the container

The UI is on port `8087`. Three volumes matter, and two of them are easy to get backwards:

- `/config` — the SQLite database, logs, backups, and the encryption key used for stored
  credentials. Back this up; everything else is replaceable.
- `/downloads` — **where downloads land.** This is the path a queue's _Local path_ points into:
  lftp writes here, and the reconciler scans here to work out what you already have.
- `/staging` — optional, and **not** a landing zone. It is the _destination_ that
  post-processing relocates a finished, verified item **to**, after it has fully downloaded into
  `/downloads`. The field is called _Final destination_ in
  [Settings → Queues](/settings/queues) for exactly that reason. Leave it unset and items stay
  where they downloaded.

`PUID`, `PGID`, and `UMASK` are honoured — the entrypoint applies them and drops root before the
app starts. Set them to match your share's expected identity if your downloads live on NFS.

## 2. Connect to the seedbox

[Settings → Connection](/settings/connection). Fill in the address, port (22 unless your
seedbox says otherwise), and username, then pick an **auth method**:

- **SSH key** — either _paste the private key_ into the form, or give a _Key path_ to a key
  file you have mounted into the container. A pasted key is encrypted at rest and decrypted only
  in memory; passphrase-protected keys are rejected, so strip the passphrase or use a key path
  instead. If both are set the pasted key wins, and the form tells you which one is actually in
  use.
- **SSH agent** — an agent socket reachable from inside the container (`SSH_AUTH_SOCK`, or the
  platform default).
- **Password** — stored encrypted. Leave the field blank on a later save to keep the password
  you already stored; it is never pre-filled back into the form.

**Known-hosts policy** decides what happens when the seedbox presents its host key. _Accept and
pin on first use_ (the default) trusts the key it sees the first time and refuses any different
key afterwards. _Strict_ only ever accepts a key you have already pinned — safest, but it will
refuse to connect until something has pinned one. _Insecure_ never verifies at all.

Use **Test connection** before saving. It reports the failure class (auth, unreachable, host
key) rather than a generic error.

## 3. Create a queue

[Settings → Queues](/settings/queues). A queue is one _remote path → local path_ mapping with
its own settings. Give it a name, the remote directory on the seedbox, and the local directory
to mirror into.

**Sync mode is the consequential choice on this page.** `copy` (the default) downloads and
never touches the seedbox. `move` downloads, verifies, and then **deletes the remote copy**.
That delete is irreversible and it happens on every item the queue finishes.

> **Warning:** Only point a `move` queue at a **hardlink pickup directory** — a directory your
> torrent client populates with links on completion. Point it at the torrent client's own
> seeding data directory and it will destroy your seeds. Saving a `move` queue requires ticking
> a confirmation that says so.

`sync` appears in the dropdown but is disabled: propagating local deletes back to the seedbox
is designed and not built.

## 4. Let it scan

Each queue is scanned on its own **scan interval**: _site default_ (30 seconds unless
`LFTPWEB_SCAN_INTERVAL_S` overrides it), 10s, 30s, 60s, or _none — on-demand only_. A scan is an
SSH round trip running `find` over the whole remote tree, so 10s is real, continuous load on a
shared seedbox.

While a queue has anything actively transferring, queued, held at the settle gate ("arriving"),
or being post-processed, an additional local-only refresh runs about every 5 seconds —
filesystem only, no SSH round trip — so progress and file-count changes on screen keep pace
between full scans. It never advances the settle gate's own two-scan check (that still needs a
real remote scan), and it never gives an _on-demand only_ queue a timer of its own.

**Rescan now**, at the top of the [Files](/transfers/files) page, forces a full pass across every queue
immediately instead of waiting. Each queue's heading shows how long ago it was last scanned, and
surfaces a scan error or warning right there rather than only in the log.

> **Note:** A queue set to _on-demand only_ has no timer at all. Auto-queue only runs at the end
> of a scan pass, so on such a queue nothing is picked up automatically until something forces a
> scan.

## 5. Queue a transfer by hand

On the [Files](/transfers/files) page, each row has a **Queue** button; select multiple rows
(shift-click for a range) to queue, stop, delete, or reset them in bulk. Watch progress on the
row's own inline bar, or on the [Transfers](/transfers/queue) page. Progress is derived from
bytes on disk
versus the known remote size, so a stopped transfer resumes from its partial rather than
restarting.

A manual Queue click always wins: it is not filtered by auto-queue suppression, and it is not
held back by the settle gate's eligibility check. It is _not_ a way to skip the settle gate's
completion check — see [Concepts](/docs/concepts).

## 6. Then, optionally

Everything below defaults to off. Turn on one at a time and watch what it does before adding the
next.

- **Auto-queue and patterns** — [Settings → Queues](/settings/queues), per queue. Turn on
  auto-queue, then add `select` / `skip` / `file_exclude` patterns; the editor previews what
  each one would match against the tree you actually have. _Patterns-only_ changes the meaning
  of having no `select` pattern from "match everything" to "match nothing".
- **Post-processing** — [Settings → Post-processing](/settings/post-processing) holds the
  site-wide default for verify, extract, delete-archives-after-extract, and
  move-to-final-destination. Each queue then inherits or overrides those individually.
- **Folder prefix during transfer** — [Settings → Transfer](/settings/transfer) (site-wide) and
  Settings → Queues (per-queue override). **On by default** — one of the few things in lftpweb
  that is, because it fixes a defect rather than adding a preference. A directory item downloads
  into a hidden-by-convention folder (`.downloading-<name>` by default, configurable) and is
  renamed to its real name only once the transfer is complete _and_ post-processing (verify, then
  extract) has finished successfully, so an importer that skips dot-prefixed folders (Sonarr,
  Radarr, Plex, Jellyfin, ...) can never see a partial multi-file release mid-arrival, nor one
  that downloaded cleanly but turned out corrupt or failed to extract — those stay hidden under
  the prefixed name until a retry succeeds. Single-file downloads are unaffected — there's no
  partial window for that shape.
- **The settle gate** — [Settings → Transfer](/settings/transfer). This one is _on_ by default,
  unlike everything else here. Read the settle-gate section under
  [Concepts](/docs/concepts) before switching it off.

> **Note:** **Local retention** (delete local copies older than N days) and **orphan temp-file
> cleanup** exist and work, but have **no Settings page yet** — they are configured only through
> the API (`PUT /api/settings/retention`, which also has a dry-run
> `POST /api/settings/retention/preview`, and `PUT /api/settings/orphan-temp-cleanup`). Both
> default off and both run on an hourly sweep; neither has a "run now" trigger.
