---
name: 2026-08-22-rtorrent-connector
status: done
created: 2026-08-22
model: sonnet
completed: 2026-08-22
result: "core/clients/rtorrent.py + tests/fake_rtorrent.py + tests/test_clients_rtorrent.py built against TORRENT_BASELINE; endpoint measured (/RPC2, direct XML-RPC); spec §13.6 correction list added (11 entries, risk-ranked); docs/decisions.md entry recorded. Full gate: 1867 passed, ruff check clean, ruff format clean."
---

# Task: The rTorrent connector — the second real adapter, and the first torrent one

Build `core/clients/rtorrent.py` against the stage 0 framework, plus `tests/fake_rtorrent.py` and
its tests. This is the connector `docs/torrent-manager-spec.md` (#21) ultimately depends on, and
the first one to exercise `TORRENT_BASELINE`.

## Measured facts about the target deployment (probed 2026-08-22, not guessed)

All behind **HTTP Basic auth**:

| Path | Result |
|---|---|
| `/RPC2` | 401, `Basic realm="ruTorrent Private Area"` — direct XML-RPC, **the chosen default** |
| `/rutorrent/plugins/httprpc/action.php` | 401, same realm — ruTorrent's plugin |
| `/xmlrpc` | 401, **different realm** (`"Private Area"`) — a separate nginx location |
| `/rutorrent/plugins/rpc/rpc.php` | **404** — does not exist |

**Use direct XML-RPC at `/RPC2` by default, and make the path a `ConfigField`** so a deployment
can point at `/xmlrpc` or the plugin without a code change. This settles
`docs/torrent-manager-spec.md` §10.1's open question — and note it is now a *cheap* choice to
reverse, because §10's SSH-based deletion removed the `erasedata` plugin from the critical path
entirely, which was the only thing that made the endpoint decision consequential.

## Before you start

1. **`docs/download-client-framework-spec.md`** — §2 (both vocabularies), §3 (phase totality),
   §4 (declaration + error taxonomy), §5 (**`TORRENT_BASELINE` is your starting point — see the
   exact per-key table**), §7 (identity, infohash normalization), §13.2/§13.3 (the fixture trap
   and capture discipline), §13.4 (SAB's correction list — **you are writing rTorrent's
   equivalent**).
2. **`core/clients/sabnzbd.py`** — the pattern to follow, *including its corrected error
   handling*. Note how measured facts are marked distinctly from doc-derived guesses.
3. **`core/clients/errors.py`** — the four error types, including the new
   `ClientAuthenticationFailed`. rTorrent's Basic-auth rejection is a **401**; treat it as an
   authentication failure, not a generic `ClientError`.
4. **`docs/download-client-api-survey.md`** §1, §2, §3's rTorrent bullets — every claim there is
   vendor-doc-derived and unverified.
5. **`tests/fake_sabnzbd.py`** — the fixture shape (real uvicorn socket, mutable state).

## Transport

Use **`httpx` + `xmlrpc.client.dumps()`/`loads()`** — build the XML-RPC body with the stdlib
marshaller and POST it over the existing async client. **No new runtime dependency**, and no
`xmlrpc.client.ServerProxy` (it is synchronous and would block the event loop).

HTTP Basic auth via httpx's own `auth=`. A **401 raises `ClientAuthenticationFailed`**; a
transport error raises `ClientUnreachable`; an XML-RPC `Fault` raises `ClientError` (or
`CapabilityUnavailable` where the fault genuinely means "this command does not exist here" —
judge it, and document the choice).

## What to build

Register as `client_type = "rtorrent"`, `family = "torrent"`, starting from `TORRENT_BASELINE`.

**Listing** — `d.multicall2` against the `main` view, requesting the fields each declared `Field`
needs in one round trip. Do **not** issue per-torrent calls to build the list; that is what
`list_trackers` is separately for (spec §2.1: it is its own operation precisely because it is an
N-call fetch).

**The traps the survey names, all of which must be handled explicitly:**

- **`d.ratio` is per-mille — divide by 1000.** A rule comparing it raw against `1.0` treats every
  torrent as wildly over-seeded. Put the divisor in one place with a comment.
- **There is no seed-time field.** Derive it from `d.timestamp.finished` and declare
  `Field.SEED_TIME_S` as `Support.DERIVED` **with a `note`** saying it measures wall-clock since
  completion, so a stopped torrent still accrues. Spec §4.3 exists for this exact case; a UI
  showing it identically to a native value is the failure the tri-state design prevents.
- **Infohashes come back uppercase.** Route every one through `normalize_client_id` (spec §7.1).
- **`d.free_diskspace` is the *minimum* across the devices an item's files span** — note it where
  `free_space` is implemented.

**`raw_status` needs a decision, and it is the interesting one.** rTorrent has no status *string*:
state is spread across `d.state`, `d.complete`, `d.is_active`, `d.hashing` and `d.message`. The
framework requires `raw_status` as a mandatory field (spec §2.2) whose contract is "the client's
own word, preserved for display." **Decide what to synthesize, and write down why in
`docs/decisions.md`** — a composed token is defensible, silently inventing SAB-like vocabulary is
not. `d.message` is the closest thing rTorrent has to an error string; `Field.ERROR_MESSAGE`
should carry it.

**`map_phase` must be total** (spec §3) and derived from those flags rather than a lookup table:
hashing → `VERIFYING`; complete + active → `SEEDING`; complete + inactive → `COMPLETED`;
incomplete + active → `DOWNLOADING`; incomplete + inactive → `PAUSED`/`QUEUED`. **Any combination
you are unsure about maps to `UNKNOWN`, never to a confident guess** — an unknown phase costs
nothing, a wrong one blocks work.

**`remove` unregisters only** — `d.stop` then `d.erase`. **It must never attempt to delete data.**
Do not implement `d.custom5.set`/`d.delete_tied` or anything resembling the `erasedata` hook
sequence; spec §10.1 removed that from the design deliberately and lftpweb deletes bytes over SSH.

**`list_base_paths`** — `directory.default`, reported as `BasePathKind.WORKING`. Per spec §1.1,
rTorrent will *not* report the completed folder it hardlinks into, and that is expected and
correct: that folder is a queue's `remote_path`, already known to lftpweb.

**`content_path`** — `d.base_path` (or `d.directory`; pick one, explain the choice). Per spec
§1.1 this is the **seeding** location, not the hardlinked completed copy — the completed copy is
invisible to rTorrent's API, which is precisely why spec §11.1b matches on inode rather than path.

## The discipline — this is the third time, do not make it the fourth

**Everything you write past the 401 is vendor-doc guesswork.** There are no credentials, so
nothing here can be verified against the live instance. This repo has now been bitten **twice** by
a fixture encoding the same wrong assumption as the code it tests (`IMPORT_EVENT_TYPES = {3}`,
which reached production; and SABnzbd's auth shape, caught by a user on day one — see §13.4).

So:

- **Every mapping and constant carries `doc-derived, UNVERIFIED against a live rTorrent,
  2026-08-22` in its own comment.** Where something is measured (the endpoints, the 401), mark it
  **MEASURED** and distinguish it clearly.
- **Add a new section to `docs/download-client-framework-spec.md`: `§13.6 The rTorrent correction
  list`**, in the same numbered, risk-ranked table form as §13.4, enumerating every guess so it
  can be worked through against real responses. Rank them — the ones that decide *terminal vs
  non-terminal* phase and *what a delete targets* matter far more than cosmetic mappings.
- `tests/fake_rtorrent.py` inherits every one of those guesses. **Say so in its docstring.** A
  green suite here proves internal consistency, not correctness.

## Tests

Registry conformance (the existing suite picks it up automatically); `map_phase` totality
including unknown flag combinations; per-mille ratio conversion; derived seed-time carrying its
note; uppercase infohash normalization; a 401 raising `ClientAuthenticationFailed`; an XML-RPC
fault raising the right type; `remove` issuing stop+erase and **never** a data-deleting call
(assert on what the fake received); `list_base_paths` returning `WORKING`.

## Verification gates — read `CLAUDE.md`

**NEVER background a gate** — pass an explicit timeout of at least 600000 ms on every gate Bash
call. Four agents on this feature have now hit this. **Run backend gates from the REPO ROOT**;
if you `cd`, `cd` back.

1. `uv run pytest` · 2. `uv run ruff check .` · 3. `uv run ruff format --check .`

## When done

Update frontmatter, `git mv` to `prompts/done/`, record decisions in `docs/decisions.md`.
**Do not commit or push.** Report: files, the three exit codes, the backend test count, a proposed
one-line `feat:` message, **the full §13.6 correction list you wrote**, and anything in the spec
found wrong or underspecified.
