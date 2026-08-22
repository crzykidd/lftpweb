---
name: 2026-08-22-client-framework-stage1a-sabnzbd
status: completed          # pending | completed | failed
created: 2026-08-22
model: sonnet            # coding
completed: 2026-08-22
result: SABnzbd connector, redacted capture helper, fake SABnzbd fixture, and tests built; ruff check/format clean; targeted tests (89) verified in foreground; full-suite pytest run was backgrounded in error and re-verified by the coordinator directly.
---

# Task: Stage 1a — the SABnzbd connector, its fake, and the redacted capture

Build the **first real connector** against the stage 0 framework: `core/clients/sabnzbd.py`, a fake
SABnzbd fixture in `tests/fake_sabnzbd.py`, and the redacted response-capture helper spec §13.3
requires.

**Stage 1 is being executed in two halves. This is 1a — the connector only.** No database table, no
migration, no settings API, no frontend, no poller. Stage 1b adds the instance row, the settings
page and the README write-up. Do not build any of it here.

## Before you start

Read, in this order:

1. **`docs/download-client-framework-spec.md`** — the governing document. Sections that bind this
   task: §2 (both vocabularies), §3 (the phase vocabulary and its totality rule), §4 (the
   declaration and the error taxonomy), §5 (**the exact baseline table** — `USENET_BASELINE` is
   this connector's starting point), §7 (identity, `normalize_client_id`), §13 (testing, and
   §13.3's capture-first discipline).
2. **`backend/lftpweb/core/clients/`** — the stage 0 framework you are building against. Read
   `base.py` fully. Note `project_transfer()`: **every `Transfer` this connector returns must go
   through it**, which is how spec §2.2's "never declare a field you can't populate" is enforced
   structurally rather than by discipline.
3. **`docs/download-client-api-survey.md`** — §1's matrix row for SABnzbd and §3's SABnzbd bullet.
   The useful specific: **disk space rides the queue call** (`diskspace1`/`diskspace2` plus
   `diskspacetotal1`/`diskspacetotal2`), making it the cheapest free-space source of the five —
   `free_space` should reuse the queue response rather than issue a second request where it can.
   **Everything in that survey is vendor-doc-derived and explicitly unverified.**
4. **`core/arrclient.py`** — the house style: narrow projection plus `raw`, `httpx.AsyncClient`
   constructed per use with a 10s timeout, dense docstrings that say *why*, pagination that trusts
   the server's own reported total rather than `page * PAGE_SIZE`.
5. **`tests/fake_arr.py`** — the fixture shape to copy: a real FastAPI app on a real uvicorn socket
   with a mutable state object a test drives in-process between passes. Not a mocked transport.

## Working tree check

Run `git status --porcelain` first and cross-reference. The tree should be clean at `bd043be`;
surface anything unexpected before editing. This prompt file is exempt.

## What to do

### 1. `core/clients/sabnzbd.py`

Register as `client_type = "sabnzbd"`, `family = "usenet"`. Start from `USENET_BASELINE` and
`.overridden(...)` only where SABnzbd genuinely differs — spec §5's table is the baseline's
contents, and the mechanism exists so this is a handful of lines, not a 25-entry rewrite.

- **Auth is an `apikey` query parameter**, with `output=json`. This is the thing §13.3's redaction
  exists for: the secret is in the URL, so it lands in any naive log line.
- **Two sources, per spec §2.1**: `mode=queue` (in-flight) and `mode=history` (finished/failed).
  `list_transfers(active_only=True)` reads the queue; the full form merges both. History carries
  the `storage` field — **the real on-disk path after rename and unpack**, which is `content_path`
  and the only trustworthy identity source (spec §7.2: never predict a path from a release name).
- **`map_phase` must be total** (spec §3). Map SAB's queue vocabulary (Downloading, Paused,
  Queued, Repairing, Extracting, Moving, Verifying, Fetching, Grabbing…) and its history statuses
  (Completed, Failed) onto `TransferPhase`. **Anything unrecognised → `TransferPhase.UNKNOWN`, and
  it must never raise.** Keep the client's own word in `raw_status`.
- **`error_message`** from history's `fail_message` — this is the explicit-failure signal spec §4.2
  turns on, and the only thing that may ever withhold work. Silence is not a verdict.
- **`free_space`** from the queue response's `diskspace*` fields where already fetched.
- **Errors**: only the three types in `core/clients/errors.py` may escape. A transport failure or
  non-2xx is `ClientUnreachable`/`ClientError` — **never** `CapabilityUnavailable`, which would
  wrongly degrade a capability (spec §4.2, and stage 0's `degrade_from_error` is what depends on
  this being right at the raise site).
- Declared `ConfigField` schema (spec §8.1) for stage 1b's generic form: base URL, API key as
  `kind="secret"`, and whatever else SAB genuinely needs. Nothing speculative.

### 2. The redacted capture helper (spec §13.3)

A small, well-tested function — put it in `core/clients/capture.py` — that turns a raw response
into something safe to write to the log:

- **Redacts the API key wherever it appears**, query string included, before anything is written.
- **Caps the sample size**, the way `core/supportbundle.py` caps its *arr log fetch.
- Generic enough that stage 2's connectors reuse it. **It must also redact announce URLs**
  (spec §7.3 — they embed per-user passkeys; hostname only, never the full URL), because the
  rTorrent connector will feed the same helper and a passkey in a debug log is a credential leak.
- Redaction happens **at the point of capture**, never "later, before display."

This is what makes fixtures correctable from reality once stage 1b deploys. Its own tests must
include a key that appears in a URL, in a body, and twice in one string.

### 3. `tests/fake_sabnzbd.py`

`fake_arr.py`'s shape: real uvicorn socket, mutable `FakeSabState` a test manipulates between
calls. Must be able to model:

- A normal queue and a normal history.
- **A blank/empty queue response** — the v0.2.4 production incident, and the reason spec §4.2
  exists. `fake_arr.py`'s `queue_empty_for_requests` is the precedent to copy.
- A failed item with a `fail_message`.
- An unrecognised status string, so `map_phase`'s totality is tested against the real transport
  rather than only as a unit.

### 4. Tests — `tests/test_clients_sabnzbd.py`

Cover: parsing both modes over the real socket; `map_phase` totality including the unknown case;
the capability declaration matching what the connector can actually populate (drive it through
`project_transfer`); `content_path` coming from history `storage`; free space off the queue call;
the blank-queue response producing *no verdict* rather than an empty-means-failed reading; and
every error path raising the correct one of the three types.

## The discipline that matters most here

**Every fixture and every mapping table in this task is authored from vendor documentation and is
UNVERIFIED against a real SABnzbd.** This repo has already shipped a defect of exactly this shape:
`IMPORT_EVENT_TYPES = {3}` was wrong, and the fake *arr encoded *the same wrong guess*, so every
test stayed green while two live Sonarr imports were misclassified `gone`.

So:

- **Every fixture and every status-mapping constant carries its provenance in its docstring**, in
  these words or close to them: *authored from vendor docs 2026-08-22, UNVERIFIED against a live
  instance.* When stage 1b deploys and the capture returns real bytes, those markers are the list
  of things to go correct.
- **Do not present a doc-derived mapping as confirmed** in a docstring, a comment, or the report.
- Where the docs are ambiguous, prefer the tolerant reading (`UNKNOWN`, `None`) over a confident
  guess — an unknown phase costs nothing, a wrong one blocks work.

## Conventions to honor

- `from __future__ import annotations` everywhere; match `core/arrclient.py`'s docstring density.
- No new runtime dependency — `httpx` is already in use.
- Record non-obvious decisions in `docs/decisions.md`, newest at top.

## Verification gates — read `CLAUDE.md`

**Never background a gate.** Foreground, generous explicit timeout. A spawned subagent gets no
background-completion notification and will stall forever. **Run from the REPO ROOT, never
`backend/`** — from `backend/` pytest collects zero tests and exits 0, which looks like a pass.

Run each separately and read its exit code:

1. `uv run pytest` (~4.5 min; allow at least 600000ms)
2. `uv run ruff check .`
3. `uv run ruff format --check .`

`ruff check` passing is not `ruff format --check` passing.

## When done

1. Update this file's frontmatter (`status`, `completed`, `result`).
2. `git mv` it to `prompts/done/` (or `prompts/failed/`).
3. Record decisions in `docs/decisions.md`.
4. **Do not commit.** Report back: files, the three exit codes, the backend test count, a proposed
   one-line `feat:` message, **an explicit list of every mapping/fixture that is doc-derived and
   needs live confirmation**, and anything in the spec you found wrong or underspecified.

Never `git add -A`, never push. Branch is `dev`.
