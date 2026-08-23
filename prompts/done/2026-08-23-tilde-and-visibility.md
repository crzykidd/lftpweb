---
name: 2026-08-23-tilde-and-visibility
status: done
created: 2026-08-23
model: sonnet
completed: 2026-08-23
result: >
  Part 1: `~` base paths resolved against the SSH home and offered (never applied automatically)
  in the not_found/unverified boxes, layered at the detection layer
  (`core.clients.detection._resolve_tilde_candidate`); a `~` path is never stored -- `client_path`
  retains it, `path` is always the confirmed absolute form. Part 2: an unattributed-clients
  banner, per-instance last-poll status (migration 029), and a one-time first-success audit event
  make a working client visible without a per-poll event. Also fixed: the settle-gate skip's
  off-by-default state is now stated in the settle-wait sentence itself (finding #5). All gates
  green: 2000 backend tests, 736 frontend tests, ruff/format/lint/build clean.
---

# Task: Resolve `~` paths against the SSH home, and make a working client visible

Fixes findings **#1** and **#2** in `prompts/test-findings-2026-08-23.md`, plus the client-events
observation recorded under #2. **Read both findings first** — they carry the live evidence and the
user's own proposed resolution for #1.

Two parts. They ship together because both are about lftpweb failing to say what it knows.

---

## Part 1 — `~` paths (finding #1)

**Observed:** rTorrent reports its base path as `~/downloads/rtorrent`. The UI says *"which doesn't
exist over SSH. Which path is it here?"* — but it does exist, at `/home/crzykidd/downloads/rtorrent`.

**Cause:** `core/browse.py` handles `~` two different ways.

- `resolve_remote_dir` — resolves `~`/relative paths via SFTP `realpath` (its own docstring).
- `remote_directory_error` — a plain `await sftp.stat(path)`, **no expansion**. This is what the
  base-path verification calls, so it stats the literal string `~/downloads/rtorrent`, which no
  SFTP server expands, and correctly reports "not found" for a path it never looked at.

### The user's resolution — offer the expansion, don't ask blind

> *"You are always connecting from a user context. If we can get home dir and pwd, we should give
> an option in the box with a note that says: It appears your ~ path pwd is xxx."*

The `not_found` box for a `~`/relative path should **pre-fill the resolved candidate and explain
it**:

> rTorrent reports `~/downloads/rtorrent`. Your SSH home is `/home/crzykidd`, so this is probably
> `/home/crzykidd/downloads/rtorrent`.

Confirmed by the user, **not applied automatically** — the same propose-don't-apply rule base-path
detection and category binding already follow. It is always resolvable: an SSH session always has a
user and therefore a home, and `sftp.realpath(".")` is already used by `resolve_remote_dir`.

### The constraint that matters most

**A `~` path must never be what gets stored.** Every downstream consumer inherits the expansion
problem — the disk-review walk roots, and stage 5's containment check that authorises `rm -rf`. A
containment check comparing `~/downloads/rtorrent` against `/home/crzykidd/downloads/rtorrent`
matches **nothing**, and a delete boundary that silently matches nothing is a bad way to fail.

Store **absolute** at confirm time; keep the client's own `~` form in `client_path`, which is
exactly what that column exists for (spec §8.2).

**Decide deliberately** (do not just make the two browse functions identical): they differ on
purpose today — `resolve_remote_dir` falls back gracefully for a half-typed field,
`remote_directory_error` gives a real answer so a typo is caught at save. Expanding `~` once at the
detection layer may be cleaner than changing either.

Add this to spec §13.6 as an rTorrent correction-list row if `directory.default` returning a tilde
form is not already there — it is exactly the kind of thing that would never surface without a live
instance.

---

## Part 2 — a working client is currently invisible (finding #2)

**Measured live:** two enabled, authenticating clients (SAB 5.1.1, rTorrent 0.9.8) produced **zero
events** and **zero Preflight rows** between them, because neither had a category mapping and an
unattributable row is *silently omitted*.

**That rule is right for the *arr source and wrong here.** For the *arr, an unattributable queue
record is genuinely noise. For a configured, authenticating, explicitly-enabled download client,
silence is the worst outcome: **a fully-working client is indistinguishable from a broken one**, and
nothing anywhere says why.

### What to build

- **Surface "this client reports N items, none attributable to a queue."** The mount-gate banner is
  the existing precedent for the shape — *one line per affected client, not one per dropped row*.
  Put it where a user would look: the Clients row, a Preflight banner, or both.
- **Show each client's last poll outcome on its Clients row** — not just its last *test*. An
  instance whose credential broke after setup currently looks fine on screen while failing every
  cycle; the user only found this by reading logs. A failed last poll should be visibly red, and
  `client_auth_failed` should read as "credential rejected", not as "unreachable".
- **At least one positive signal that the integration is alive.** Today the only proof is it
  breaking. A first-successful-poll event per instance, or a last-successful-poll timestamp on the
  row, or both.

**Do NOT emit a per-poll event.** A 10-second cadence would bury the log — which is precisely why
`core/arrsync.py` doesn't do it either. Events mark *transitions*, not heartbeats.

### Also worth fixing while here

Finding #5 notes that "we went into settling and SAB said nothing" is **indistinguishable from the
settle-gate skip simply being off** (it ships off by default, stage 2b). Wherever settling is
surfaced, say when the client-verdict skip is disabled rather than leaving the user to infer it.

---

## Tests

- A `~` base path is resolved against the SSH home and **offered**, not auto-applied.
- The stored path after confirming is **absolute**; `client_path` retains the `~` form.
- A genuinely missing path still reports `not_found` — the fix must not turn every miss into a
  false suggestion.
- `unverified` (stat failed for another reason) stays distinct from `not_found`. Do not collapse
  them; `remote_directory_error` draws that line deliberately.
- A client with items but no category mappings produces the "reports N, none attributable" signal.
- A client with a failing last poll renders distinguishably from one that has never polled.
- **No per-poll event is written on a successful pass** — assert the log stays quiet.

## Verification gates — read `CLAUDE.md`

**NEVER background a gate** — explicit timeout of at least 600000 ms on every gate Bash call.
**Run backend gates from the REPO ROOT**; if you `cd` into `frontend/`, `cd` back.

1. `uv run pytest` · 2. `uv run ruff check .` · 3. `uv run ruff format --check .`
4. `npm run build`, `npm run lint`, `npm test` from `frontend/`

## When done

Update frontmatter, `git mv` to `prompts/done/`, record decisions in `docs/decisions.md`, update
the spec where §8.2/§13.6 are affected, and append resolutions under findings #1 and #2 in
`prompts/test-findings-2026-08-23.md`.
**Do not commit or push.** Report: files, every exit code, both test counts, a proposed one-line
message, how you decided the `~` expansion should be layered, and anything else found.
