---
name: 2026-08-17-support-bundle
status: done
created: 2026-08-17
model: sonnet
completed: 2026-08-17
result: >
  Shipped end to end: POST /api/support-bundle (api/support_bundle.py + core/supportbundle.py),
  ArrClient.log_files/download_log_file, SupportBundleDialog.tsx + lib/supportBundle.ts, docs
  (CHANGELOG, decisions.md, concepts.md, startnewsession.md row Y). Backend 1276 / frontend 438
  tests, 0 skipped; all gates green. Unviewed in a browser (no browser in this environment).
---

# Task: Support bundle — a downloadable diagnostic zip from Settings → Logs

User request, design settled 2026-08-17: Settings → Logs gains a "Support bundle" button
opening a dialog of checkboxes for what to include, producing one downloadable **zip** the
user can attach to an issue or send manually. (The user said "rar" — settled as zip: RAR
creation is proprietary, the image has no rar binary, and Python's stdlib `zipfile` needs no
new dependency. Note this substitution in the decisions entry.)

## Bundle contents (settled) — one checkbox each, all default ON

1. **lftpweb logs** (REQUIRED — checkbox shown checked + disabled): the live log file plus
   every rotated file, exactly the set `api/logs.py` already lists. The credential redactor
   already ran on the way in (`logsetup.py`), so log content is safe as-is.
2. **Build + environment snapshot** (`bundle/environment.json`): version, build SHA/channel,
   migration level, the `/api/health` payload, lftp/Python versions, and per-queue disk usage
   (`shutil.disk_usage` on each queue's `local_path`/`staging_path`, errors captured as
   strings — a missing mount is itself diagnostic).
3. **Sanitized settings dump** (`bundle/settings.json`): host config (address/port/username/
   auth method — NEVER password, key text, or key path contents), queues (paths, modes, every
   toggle, *arr bindings), patterns, transfer/postprocess/backup settings, auth MODE only,
   *arr instances (name, kind, base_url, enabled, notify flag — NEVER the API key, not even
   encrypted). **Build this from the existing API response models** (`HostOut`,
   `QueueOut`, `ArrInstanceOut`, ...) which are already write-only for secrets — do not
   hand-pick columns from the DB, which is exactly how a future field leaks. Add a test that
   asserts the bundle's settings JSON never contains the seeded password/API-key/key-material
   strings.
4. **Recent audit trail** (`bundle/events.ndjson`): the most recent 1000 `event` rows,
   newest first — reuse `api/history.py`'s query shape, not raw SQL.
5. **Recent job history** (`bundle/jobs.ndjson`): the most recent 100 jobs with error class
   and `output_tail`.
6. **Sonarr/Radarr log files** — one checkbox per **enabled** *arr instance (hidden
   entirely when none are enabled): fetched via the *arr v3 API (`GET /api/v3/log/file` to
   list, each file's download endpoint to fetch) through a new small method pair on the
   existing `core/arrclient.py.ArrClient`. Per-instance failure (unreachable, bad key) must
   not fail the bundle — write `bundle/arr-<name>/FETCH-FAILED.txt` with the error instead,
   and cap per-instance fetch at a sane byte budget (~20 MB) noting truncation the same way.

**Deliberately excluded, named in code + decisions entry:** the SQLite database (carries
encrypted secrets and the encryption landscape; migration level + settings dump cover what
support needs). The `known_hosts` pins and the install secret, obviously. No redaction pass
is attempted on *arr logs — they're the *arr's own logs, the user chooses to include them.

## Implementation shape

- **Backend** (`api/support_bundle.py` + `core/supportbundle.py`, following the repo's
  api-thin/core-testable split): `POST /api/support-bundle` takes the checkbox selection,
  streams a zip back (`zipfile` + an in-memory or spooled temp file — bundles are small; the
  logs budget dominates and is already bounded by rotation). Auth-gated automatically; add
  the route to `tests/test_auth_api.py.PROTECTED_ROUTE_TEMPLATES`. Bundle filename:
  `lftpweb-support-<version>-<UTC timestamp>.zip`.
- **Frontend** (`pages/settings/LogsTab.tsx` + a small dialog component): "Support bundle…"
  button → checkbox dialog (lftpweb logs checked+disabled; the rest per the list; *arr rows
  only when enabled instances exist, resolved via the existing `listArrInstances()` client
  call) → Generate downloads the response as a file. Keep selection logic in pure helpers
  with Vitest coverage per the repo pattern.
- A generated bundle writes ONE `info` event (`support_bundle_created`, naming the selected
  parts) — support asks "when was this bundle made and what's in it" constantly, and the
  audit trail is where this project answers such questions.

## Working tree check

Run `git status --porcelain` before editing; cross-reference the files this plan touches. If
any have uncommitted changes, list them and ask before touching. This file is exempt.

## Before you start

- `DESIGN.md` §8 (auth), §10 (ops precedents — the log/backup download endpoints are the
  closest existing shapes; note their anchored-filename regexes are security controls resting
  under dismissed CodeQL alerts — do not weaken them, and follow their patterns for any new
  file-serving path).
- `api/logs.py` (rotated-file listing + download), `api/backup.py` (file streaming),
  `core/arrclient.py` (client conventions, the *arr API base), `tests/fake_arr.py` (extend
  for the log-file endpoints).
- The S3 input-cap standard for any new request fields.

## Tests

Backend: bundle contains exactly the selected parts; the secrets-absence assertion (seeded
password + API key strings absent from every bundle byte); *arr fetch failure produces the
FETCH-FAILED marker, not a 500; unauthenticated 401 via the existing enumeration. Frontend:
pure-helper coverage for the dialog's selection/visibility rules. Full suites green.

## Docs, same commit

- `CHANGELOG.md` `[Unreleased]` → Added.
- `docs/decisions.md`: zip-not-rar, DB exclusion, secrets-from-response-models rule, the
  per-instance failure containment.
- `docs/how-it-works.md` or the Docs section page that covers ops/logs — one short paragraph
  on what a bundle contains (users will ask what they're sending).
- `prompts/startnewsession.md`: add the next build-run table row (letter after whatever the
  logs-search task used), same style, same commit. Note the dialog is unviewed.

## Verify — each gate separately, read each exit code

`uv run ruff check backend tests` · `uv run ruff format --check backend tests` ·
`uv run pytest` (full) · `npm test -- --run` · `npm run lint` · `npm run build`.

## When done

1. Update this file's frontmatter (`status`, `completed`, `result`).
2. Move this file into `prompts/done/` (or `failed/`).
3. Hand off ONE commit (prompt file + changes + prompt move). **You are a spawned agent: do
   not commit.** Prepare the tree, then report the file list + proposed `feat:` message back
   to the orchestrating session, which surfaces the `y/n`.
