# lftpweb

A containerized web interface for keeping a local directory in sync with a seedbox, using
**lftp** as the transfer engine over SSH/SFTP.

Browse the remote and local trees as one view, queue and supervise downloads with live
progress, auto-queue on patterns, and optionally verify, extract, and relocate finished items.

> ## ⚠ Pre-release — not ready to use
>
> **Version `0.0.1`. 3 of 9 build phases complete.** There is no release, no published image,
> and no upgrade path. Things will change without notice, including the database schema.
>
> Try it if you want to poke at it. Don't point it at anything you care about yet.

## What works today

- Connect to a seedbox over SSH/SFTP; browse the remote tree alongside the local one
- Named **path queues** — one remote → local mapping each, with their own settings
- Queue transfers manually, watch live progress, stop them, resume from the partial
- Bandwidth ceiling and concurrency limits with an admission-control scheduler
- Credentials encrypted at rest

## What doesn't yet

| | Phase |
|---|---|
| Auto-queue and include/exclude patterns | 4 |
| Archive extraction, checksum verification, staging → final move | 5 |
| History view | 6 |
| Log viewer, scheduled database backups | 7 |
| Authentication (**the UI is currently unauthenticated**) | 8 |

Propagating local deletes back to the seedbox (`sync` mode) is designed but **not scheduled** —
see `DESIGN.md` §7.

## Running it

Requires Docker and a seedbox you can reach over SSH.

```bash
docker compose up -d          # production-shaped
docker compose -f docker-compose.dev.yml up --build    # local development
```

The UI is on **`8087`** (`5187` for the Vite dev server). Configure the host and your path
queues under **Settings**.

| Volume | Holds |
|---|---|
| `/config` | SQLite database, logs, backups, encryption key |
| `/downloads` | where files land |
| `/staging` | optional; download here, move to `/downloads` when complete |

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

The container image also bundles unmodified third-party programs (lftp, OpenSSH, 7-Zip, su-exec,
tini), each under its own licence. lftpweb runs them as separate processes rather than linking
against them. See [`NOTICE`](NOTICE).
