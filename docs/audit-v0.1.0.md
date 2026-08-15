# Post-v0.1.0 audit — security, partitioning, settings/gating

**Date:** 2026-08-14 · **Scope:** the whole codebase as of `40e1ae3` (dev, clean tree) ·
**Author:** audit session, uncommitted for review.

This is a findings-and-priorities report, not a set of applied changes. Nothing in the tree was
modified. Each item below says what it is, why it matters, and roughly what fixing it costs, so
we can pick an order together. Where a claim was *verified* (not just read), it says so.

---

## TL;DR priority table

| # | Area | Finding | Severity | Effort |
|---|---|---|---|---|
| **S1** | Security | **Unauthenticated arbitrary file read** via the SPA catch-all route (`spa_fallback`) — path traversal, verified exploitable | 🔴 **Critical** | XS (a few lines) |
| S2 | Security | Archive extraction does not containment-check member paths (zip-slip relies entirely on 7zz/unrar) | 🟠 Medium | S |
| S3 | Security | No input length caps on credentials/free-text; no `port` bounds → argon2/body-size DoS surface | 🟡 Low–Med | S |
| S4 | Security | No security response headers (CSP, X-Content-Type-Options, X-Frame-Options) | 🟡 Low | XS |
| G1 | Gating | `move`-mode remote delete runs **before** extraction — a `SKIPPED`-verify release whose extract later fails is already deleted (this is open issue #2) | 🟠 Medium | M (design call) |
| G2 | Gating | `net:connection-limit` still has no write path (known gap) — the one §4.5 "first-class setting" unreachable from any UI | 🟡 Low–Med | M |
| G3 | Gating | Several settings lack server-side bounds/validation (retention counts, ports, blank names) | 🟡 Low | S |
| P1 | Partition | `FileTree.tsx` is **2267 lines** — the worst "read everything to change one thing" file in the repo | 🟠 High value | M |
| P2 | Partition | `api/settings.py` (1068) is one router doing 10 resources — split into sub-routers like `api/auth.py`/`api/backup.py` already are | 🟠 High value | S–M (mechanical) |
| P3 | Partition | `core/local_delete.py` (1649) is four unrelated features in one file (delete, retention, archive-cleanup, reset) | 🟠 High value | M |
| P4 | Partition | `core/queue.py` (1881) mixes admission, reaping, progress, metrics, settings | 🟡 Med value | M–L |
| P5 | Partition | `core/engine.py` (1621) — the persist/project half (~500 lines) can leave the loop orchestrator | 🟡 Med value | M |

Recommended first cut: **S1 now** (tiny, critical), then **P2 + P1** (biggest token-cost wins,
low risk), then work the rest.

---

## 1 · Security

### The good news first

The obvious attack surfaces are handled well, and it's worth recording so we don't "re-fix"
things that are already right:

- **No SQL injection.** Every query is parameterized; grep finds zero f-string/format/concat SQL.
- **No shell/command injection on the remote path.** Every remote command
  (`find`, `rm -rf --`, `python3 <tmp>`) goes through `shlex.quote`; every lftp value goes
  through `_lftp_quote` (single-quote, POSIX `'\''` doubling). `rm -rf --` additionally refuses
  empty/`/`/`.`/`..` targets. Subprocesses use `create_subprocess_exec`/`subprocess.run` with
  arg lists, never `shell=True`.
- **The two file-download endpoints** (`/api/logs/{f}/download`, `/api/settings/backup/{f}/download`)
  are anchored to `\Z`-terminated filename regexes admitting no separator — the exact controls the
  five CodeQL false-positives rest on (`b06cafe`). Still correct.
- **Auth model is sound.** Default-deny ASGI middleware over all of `/api/` with a 4-entry public
  allowlist; argon2id passwords; SHA-256 for high-entropy tokens (justified); CSRF on mutating
  methods; trusted-CIDR from the real socket peer, never a header; fail-closed on unknown mode.
- **Credentials at rest**: Fernet via HKDF, secret 0600, excluded from backups by construction.

### 🔴 S1 — Unauthenticated path traversal / arbitrary file read (CRITICAL)

**Where:** `backend/lftpweb/main.py`, `spa_fallback` (the `@app.get("/{full_path:path}")`
catch-all).

```python
candidate = static_dir / full_path
if full_path and candidate.is_file():
    return FileResponse(candidate)
```

`full_path` is request-controlled and joined to `static_dir` with **no containment check**. The
`.startswith("api/")` guard only steers around the API routers — it does nothing about `..`.

**Verified exploitable.** Using Starlette's own routing + this exact handler, a request for a
percent-encoded path escaped the static dir and returned a file's contents from outside it:

```
GET /..%2f..%2f<dir>%2fsecret.txt   ->  200   "TOP SECRET FILE OUTSIDE STATIC DIR"
```

The literal `/../../` form is normalized away by browsers/httpx, but the `%2f`-encoded form is
**not** — and a non-normalizing client (`curl --path-as-is`, or any crafted request; uvicorn
passes `..` through and percent-decodes) delivers it.

**Why it's critical, not theoretical:**
- This route is **outside the auth middleware** — the middleware only gates paths starting with
  `/api/`. So this is an **unauthenticated** read, in *every* auth mode including `password`.
- On the real container, the app's uid can read `/config/secret.key` (the credential-encryption
  key), `/config/lftpweb.db` (the whole database — session hashes, encrypted host creds, API-key
  hashes), any mounted SSH key, `/etc/passwd`, etc. Reading `secret.key` **defeats the
  credential-at-rest scheme** — combined with the DB it hands over the seedbox password.
- A TLS-terminating reverse proxy that normalizes `..` mitigates it, but the app must not depend
  on that — the design explicitly supports plain-HTTP LAN exposure with no proxy.

**Fix (verified to block the traversal while preserving SPA routing and real assets):**

```python
index = static_dir / "index.html"
candidate = static_dir / full_path
try:
    resolved = candidate.resolve()
    resolved.relative_to(static_dir.resolve())
except (ValueError, OSError):
    return FileResponse(index)          # escape attempt -> serve the SPA shell
if full_path and resolved.is_file():
    return FileResponse(resolved)
return FileResponse(index)
```

Tested: `/..%2f..%2fetc/hostname` → SPA shell (blocked); `/app.js` → real asset; `/settings/logs`
→ SPA (client route intact). Add a regression test hitting the encoded-`..` form. Effort: XS.
**This one is worth doing before anything else in the report.**

### 🟠 S2 — Extraction trusts the archiver for member-path containment (zip-slip)

**Where:** `core/extract.py.extract_archive` → `7zz x`/`unrar x -y`.

`resolve_within_root` guards the `_UNPACK_`/`_FAILED_` staging dirs and every *deletion*, but it
does **not** constrain where the extractor writes archive *members*. A malicious archive with
`../../` members or absolute paths relies entirely on 7zz/unrar's own traversal defenses to stay
inside `target_dir`. Modern p7zip and unrar do refuse `..`/absolute members, so this is defense
missing rather than a live hole — but the whole point of an audit is that "the download came from
a seedbox I control" is exactly the assumption that erodes over time (shared trackers, compromised
uploads).

**Fix options:** after extraction, walk `extracted_dirs` and assert every produced path is under
`target_dir` via `resolve_within_root` (fail the item `EXTRACT_FAILED` + audit event if not); or
run 7zz with the member list first and reject traversal names before extracting. Effort: S.

### 🟡 S3 — No length caps on credential/free-text inputs; no `port` bounds

`LoginIn.password`, `ChangePasswordIn.new_password`, host `name`/`address`/`username`/`password`,
and pattern globs have no `max_length`. `port: int` has no `ge/le`. Two consequences:

- **argon2 CPU/mem DoS**: an unauthenticated `POST /api/auth/login` with a multi-MB password body
  makes the server argon2-hash it. Login is rate-limited (5 / 300 s / IP), which bounds it, but
  each of those 5 attempts still hashes an attacker-sized input, and FastAPI sets no default body
  cap. Cheap to close.
- **Junk stored**: a `port` of `-1`/`999999`, a blank `name`, etc. are accepted today.

**Fix:** add `Field(max_length=...)` to the free-text/credential models (e.g. 1024 for passwords,
256 for names/addresses), `Field(ge=1, le=65535)` on ports, and reject blank required names
server-side. Effort: S. Pairs naturally with G3.

### 🟡 S4 — No security response headers

Only `AuthMiddleware` is installed. No `Content-Security-Policy`, `X-Content-Type-Options:
nosniff`, `X-Frame-Options`/`frame-ancestors`. For a same-origin SPA the risk is modest, but a CSP
(`default-src 'self'`) and `nosniff` are cheap defense-in-depth and would harden against a future
XSS or clickjacking. A small response-header middleware. Effort: XS.

*(Note, not a defect: the session cookie's `Secure` flag is set dynamically from request scheme —
a deliberate, documented weakening for plain-HTTP LAN use. Leave as-is.)*

---

## 2 · Settings & gating — what beta 1 didn't account for

### 🟠 G1 — `move`-mode delete happens before extraction (open issue #2)

In `postprocess._process_item` the order for a `move` queue is **verify → delete remote →
extract**. The delete gate withholds only on `CORRUPT` (correct — `6883db3`). But a release with
**no sidecar** verifies `SKIPPED`, so the remote is deleted, and *then* extraction runs — if
extraction fails (`EXTRACT_FAILED`), the only re-fetchable source is already gone. This is exactly
the question already filed as **issue #2**; flagging it here so it's on the consolidated list. The
design call: either move the remote delete to *after* a successful extract, or make the delete gate
also require extraction not to have failed (mirroring the download-prefix `release_ok` rule, which
already ANDs both). Effort: M, mostly deciding.

### 🟡 G2 — `net:connection-limit` has no write path (known gap #5)

§4.5 calls it "a first-class setting, host-level, not an advanced afterthought," but it lives only
inside the `host.connection_overrides` JSON blob with no UI or endpoint to set it, so Settings →
Transfer's live connection-count readout can compute the worst case but can never actually enforce
or warn. Needs a migration (promote to a real column) + a field in the Transfer/Connection tab.
Effort: M. Already in README "Known gaps"; raising priority now that we're past 0.1.0.

### 🟡 G3 — Missing server-side bounds on a few settings

Beyond ports/names (S3): confirm retention `keep_count`/`interval_days` (backup already checks
`>0`/`>=1` — good; check the *local-file* retention + metrics retention paths match), scan-interval
values, and download-prefix strings all reject nonsense server-side, not just in the form. Mostly
present; this is a sweep to close the last few. Effort: S.

### Defaults review — no change recommended, recorded for the record

The "every new capability defaults OFF" rule held across the build (auto-queue, remote delete,
auth, retention all default off; scheduled backup is the one deliberate, reasoned exception). That
posture is right for a self-hostable tool. The one thing worth a conscious decision at 0.1.x:
**the download-prefix feature defaults ON** (`d73e221`) — correct for safety (importers can't grab
partials), but it's the source of the logical-vs-physical-path complexity behind five past defects,
so it should stay a first-class, documented, toggleable behavior, not drift into an assumption.

---

## 3 · Code partitioning — cutting the "read 2000 lines to change one thing" cost

Goal: no single file should force a coding agent (or a human) to load its whole body to make a
localized change. The worst offenders and concrete, low-risk splits. **None of these change
behavior** — they're module boundaries drawn along seams the code already has. Do them as
mechanical moves with the tests green before and after.

### 🟠 P1 — `FileTree.tsx` (2267 lines) → ~700 + four modules

By far the highest-value split; it's the biggest file in the project and touched constantly. It
already contains cleanly separable, mostly-pure blocks (many already have tests in
`FileTree.test.ts`):

| Extract to | What moves | ~lines |
|---|---|---|
| `lib/fileTree.ts` (pure) | `buildTree`, `flatten`, `sortTree`/`compareValues`/`sortValue`, `nodeDisplaySize`, `effectiveSpeed/Eta*`, `resolveCollapsed`, `matchesFacetFilter`, the preference type-guards | ~500 |
| `lib/columnWidths.ts` + `FileTreeColumns.tsx` | `ColumnDef`/`RESIZABLE_COLUMNS`/`clampColumnWidth`/`mergeColumnWidths`/`fixedColumnStyle`/`ColumnResizeHandle` | ~250 |
| `FileTreeHoverCard.tsx` | `HoverCard*` (Handle/Body/Content/Host) | ~190 |
| `FileTreeRow.tsx` | `Row` + its `RowProps`, the per-row action buttons | ~340 |
| `lib/bulkActions.ts` + `BulkActionBar.tsx` | `BulkFailure`/`BulkOutcome`/`errorMessage` + the bulk queue/stop bar | ~150 |

Leaves `FileTree.tsx` as the container (state wiring + layout), ~700 lines. A change to sorting,
hover cards, or the bulk bar then loads one small file.

### 🟠 P2 — `api/settings.py` (1068 lines) → sub-routers (mechanical, do early)

This file is one `APIRouter` covering **host, queues, patterns, postprocess, settle,
removal-grace, download-prefix, autoqueue, retention, orphan-temp** — ten resources. The repo
*already* uses per-resource routers (`api/auth.py`, `api/backup.py`, `api/logs.py`, `api/metrics.py`)
and mounts them in `main.py`. Follow that pattern:

- `api/settings_host.py` (host + test) · `api/settings_queues.py` (queues + autoqueue-status +
  pattern CRUD/preview) · `api/settings_postprocess.py` (postprocess/settle/removal-grace/
  download-prefix/autoqueue/retention/orphan-temp).

Pure move of `@router` functions + their small helpers; mount each in `main.py`. Lowest risk of
any split (no logic, no shared mutable state), high payoff (settings changes are frequent). Do it
right after S1. Effort: S–M.

### 🟠 P3 — `core/local_delete.py` (1649 lines) → four modules

Four features share this file only by adjacency:

| Extract to | Block | ~lines |
|---|---|---|
| keep in `local_delete.py` | `delete_local`, `_physical_local_root`, `DeleteInFlight`, `reconsider_removed_state`, subtree helpers | ~560 |
| `core/retention.py` | `RetentionSettings`, `RetentionScheduler`, `_select_expired`, `preview_retention`, orphan-temp sweep + its settings | ~360 |
| `core/archive_cleanup.py` | `delete_extracted_archives`, `load/save_deleted_archive_paths`, `ArchiveCleanupResult` | ~270 |
| `core/reset.py` | `reset_item`/`reset_queue`/`reset_pattern_matches`/`reset_by_pattern` + `ResetOutcome`/`_reset_*` | ~330 |

Watch the circular-import note (`postprocess ↔ local_delete ↔ mount_sentinel` uses local imports);
`_physical_local_root` must stay the one shared resolver — keep it in the core module everyone
imports. Effort: M.

### 🟡 P4 — `core/queue.py` (1881 lines) → settings + reaping + progress out

`TransferQueue` is the transfer god-object. Reasonable seams:

- `core/transfer_settings.py` — `TransferSettings` + `load/save` + `compute_retry_backoff` (~150).
- `core/reap.py` — `_reap_one`/`_completeness_on_disk`/`_relevant_remote_total`/
  `_flush_child_progress_final` (~350), the completeness-gates-`DOWNLOADED` logic.
- `core/progress_publish.py` — `_sample_and_publish_progress`/`_publish_child_progress`/
  `_sample_metrics` (~430).

Leaves the admission/spawn/lifecycle core ~800. More care than P1–P3 (these methods touch a lot of
`self`), so schedule it after the easy wins, possibly as instance methods delegating to
module-level functions taking the DB/state explicitly. Effort: M–L.

### 🟡 P5 — `core/engine.py` (1621 lines) → persist half out

`_persist` (~400) + `_project`/`_previous_states`/`snapshot` are the persistence/projection half;
`load_host_config`/`load_queues`/`QueueConfig` are config loading. Moving the persist/project
cluster to `core/persist.py` and the config loaders to `core/queue_config.py` leaves `engine.py`
as the scan-loop orchestrator (~700). The scan→persist→read-back→diff→publish invariant must stay
intact and commented at the seam. Effort: M.

### Not worth splitting

`models.py` (950) is a single Pydantic schema module — one file is the *right* shape for that; a
localized change loads only the relevant class regardless of file size. Leave it. Same for
`QueuesTab.tsx`/`TransferTab.tsx` (big forms) unless they get touched — the patterns-editor block
inside `QueuesTab` is the one candidate (`QueuePatternsEditor.tsx`) if we're in there anyway.

---

## Suggested order

1. **S1** — the traversal fix + regression test. Small, critical, ship it on its own `fix:` commit.
2. **P2** then **P1** — the two biggest token-cost reductions, lowest behavioral risk. Pure moves,
   tests green throughout.
3. **S3 + G3 + S4** — one "input hardening + headers" pass (`fix:`), cheap and cohesive.
4. **G1** — decide the delete-order question (issue #2); implement whichever way we choose.
5. **P3**, then **P4/P5** — the deeper backend splits, one at a time, each its own commit.
6. **S2**, **G2** — extraction containment and the connection-limit write path, as scoped features.

Each of P1–P5 and G1/G2 is big enough to be its own handoff prompt + spawned agent per the repo
workflow. S1/S3/S4 are small enough to do in-session.
