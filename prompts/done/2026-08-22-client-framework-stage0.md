---
name: 2026-08-22-client-framework-stage0
status: completed        # pending | completed | failed
created: 2026-08-22
model: sonnet            # coding
completed: 2026-08-22
result: >
  Built backend/lftpweb/core/clients/ (errors.py, models.py, base.py, __init__.py),
  tests/fake_client.py, tests/test_clients_framework.py (33 tests). All gates green:
  pytest 1718 passed, ruff check clean, ruff format --check clean. Recorded in
  docs/decisions.md. Flagged to the spec: the USENET_BASELINE/TORRENT_BASELINE per-key
  mapping is this module's own reading of §5's prose, not a literal transcription, and
  the "only three error types escape" / "no field declared that cannot be populated"
  conformance checks are only registry-generic in principle until a second connector
  with controllable failure state exists (stage 1).
---

# Task: Download-client connector framework — stage 0 (the interface, no real client)

Build the pluggable connector layer that [#18](https://github.com/crzykidd/lftpweb/issues/18)
needs and that [#21](https://github.com/crzykidd/lftpweb/issues/21) depends on: the operation and
field vocabularies, the capability declaration, the registry, and the conformance suite — with a
**fake adapter only**. No real client is contacted in this stage; SABnzbd is stage 1.

**This stage ships with nothing configured and nothing user-visible.** There is no API surface, no
migration, no settings page, and no poller. That is deliberate — this is the piece whose vocabulary
is expensive to change later, so it lands on its own with tests around it.

## Before you start

**Read [`docs/download-client-framework-spec.md`](../docs/download-client-framework-spec.md) in
full — it is the source of truth for this task and it is uncommitted on `dev` right now.** Sections
that govern stage 0 specifically: §1 (the two inherited rules + the no-database-handle guarantee),
§2 (the two vocabularies), §3 (the phase vocabulary), §4 (the three-layer declaration and the
error taxonomy), §5 (baseline profiles, `family` as display-only), §6 (layout, registry,
conformance), §7.1 (infohash normalization), §13 (testing).

Also read:

- **`CLAUDE.md`** — the operating rules, in particular the verification-gate rules quoted below.
- **`core/arrclient.py`** — the house style this must match: narrow projection dataclass plus a
  `raw` dict, `from __future__ import annotations`, dense explanatory docstrings that say *why*,
  module-level constants with the reasoning attached.
- **`core/preflight.py`** — the source-agnostic boundary discipline to imitate. That module names
  no single source anywhere in code, and has survived five tasks that way.
- **`tests/fake_arr.py`** — the fixture shape (real uvicorn socket, mutable state object driven
  in-process between passes). Stage 0 does not need a real socket, but stage 1 will, so do not
  build anything that makes that harder.

**Scope discipline.** Build stage 0 only. Do not write the SABnzbd adapter, the instance table or
migration, the settings API, the poller, the delete path, or the disk scan. If the build reveals
that the spec is wrong or underspecified, **say so in the report rather than silently diverging** —
the doc gets corrected.

## Working tree check

Before making any edits, run `git status --porcelain` and cross-reference the files this plan
needs to modify. If any have uncommitted changes, list them and ask before touching them.
`docs/download-client-framework-spec.md` is **expected** to be untracked — it is this task's own
input and bundles into the same commit. Surface unrelated dirty files once as awareness; don't
block. This prompt file is exempt.

## What to do

Create `backend/lftpweb/core/clients/` — the first subpackage under `core/`, which is otherwise
flat. Justified at 7–10 adapter modules plus the framework; spec §6 flags it rather than doing it
silently.

### 1. `core/clients/errors.py` — the three-way taxonomy (spec §4.2)

`ClientUnreachable`, `ClientError`, `CapabilityUnavailable`. The docstrings must carry the reason
the split exists: **only `CapabilityUnavailable` may degrade a declared capability.** A transport
failure says nothing about what a client supports, and degrading on one would turn a bad network
minute into a permanently disabled feature. Contrast with `core/arrclient.py.ArrClientError`, which
deliberately collapses these — explain why this module does not.

### 2. `core/clients/models.py` — the normalized record shapes

- `TransferPhase` — a closed enum, exactly the nine values in spec §3.
- `Transfer` — frozen dataclass. Mandatory fields `client_id`, `name`, `phase`, `raw_status`; every
  declared field from spec §2.2 optional and defaulting to `None`; plus `raw: dict[str, Any]`
  (`field(repr=False)`, same as `QueueRecord`).
- `TrackerInfo`, `SpaceInfo` (free plus optional total — Transmission is the only client reporting
  total), `RemoveOutcome`, `BasePath`.
- `normalize_client_id()` — lowercases hex infohashes, leaves other id forms (SAB's `nzo_id`)
  untouched, per spec §7.1. One function, documented as the single place this happens, with the
  real production evidence in the docstring: the *arr hands over
  `12682AF0C00A061448BCFA16975A5D5F01A84A61` uppercase.

### 3. `core/clients/base.py` — vocabularies, capabilities, the ABC, profiles

- `Operation` and `Field` — two closed `StrEnum`s, exactly the tables in spec §2.1 and §2.2. Do not
  invent keys not in the spec, and do not omit any.
- `Support` — `NATIVE` / `DERIVED` / `NONE`.
- `Capability` — frozen dataclass of `support` plus an optional `note`. The note exists because a
  derived value's *semantics* differ; the canonical case is in the docstring: rTorrent has no
  seed-time field, and deriving it from `d.timestamp.finished` measures wall-clock since
  completion, so a stopped torrent still accrues.
- `CapabilitySet` — the merge of the three layers (static → probed → runtime-degraded, spec §4.1),
  with `supports(key, *, accept_derived=False)` as the one query callers use. Merging is
  narrowing-only: a layer may remove or downgrade support, never add it.
- `USENET_BASELINE` / `TORRENT_BASELINE` (spec §5) as pre-built sets a connector starts from and
  overrides.
- `DownloadClient` — the ABC. **Its `__init__` takes connection config only, never a database
  connection or a session factory** (spec §1). Put that guarantee in the class docstring in the
  terms the spec uses: a connector cannot write `item.state` because it has nothing to write to,
  and a future change wanting to pass a connection in is this rule being violated.
- `family: ClassVar[str]` — display metadata only, with the docstring stating plainly that it must
  never appear in a capability decision (spec §5.1).
- A declared connection-config schema per connector (spec §8.1) so settings can render one generic
  form later. Keep it minimal here — a list of field descriptors is enough; stage 1 consumes it.
- `map_phase()` as an abstract or overridable hook that is **total**: it may never raise on an
  unrecognized status, and must fall back to `TransferPhase.UNKNOWN` (spec §3).

### 4. `core/clients/__init__.py` — the registry

A decorator registering a connector class into a module-level dict keyed by `client_type`, plus
explicit imports of each adapter module. No entry-points, no dynamic import scanning — this project
ships one image and gains nothing from discovery machinery. Registering a duplicate `client_type`
is an error, not a silent overwrite.

### 5. A fake adapter for tests

`tests/fake_client.py` (or `core/clients/_fake.py` if it genuinely needs to be registered — prefer
the test-side location, and justify it either way). Enough to exercise the registry, the capability
merge and the conformance suite. **It must be able to model a connector that declares a capability
and then fails it at runtime**, so the degradation path (spec §4.1 layer 3) is testable.

### 6. `tests/test_clients_framework.py` — the conformance suite (spec §6.2)

Parameterized over the whole registry, asserting of **every** registered connector:

- Every `Operation` and `Field` key is declared — none silently missing.
- No field is declared that the connector cannot populate. (A field declared and returned `None` is
  worse than one declared absent: a consumer offers a rule that silently never matches.)
- `map_phase` is total — no input raises, unknown input yields `UNKNOWN`.
- Only the three error types escape its methods.
- The declared config schema round-trips.

Plus direct unit tests for the parts that are not per-connector:

- The three-layer merge, including **that a `ClientUnreachable` does not degrade anything** and a
  `CapabilityUnavailable` does. This is the single most important test in the stage.
- `supports(..., accept_derived=...)` in both modes.
- `normalize_client_id` on an uppercase infohash, a lowercase one, and a SAB `nzo_id`.
- Baseline profile override semantics.
- Duplicate registration raising.

## Conventions to honor

- Match `core/arrclient.py`'s docstring density and tone: say *why*, cite the spec section
  (`spec §4.2`) or `docs/decisions.md`, and record the real-world evidence where there is any.
- `from __future__ import annotations` at the top of every module.
- No new runtime dependency. Everything here is stdlib plus what the project already has.
- Record non-obvious decisions in `docs/decisions.md`, newest at top — at minimum: the
  operation-versus-field split, why `remove_with_data` is absent from the vocabulary (spec §10.1),
  the no-database-handle structural guarantee, and `core/clients/` being the first subpackage under
  a previously flat `core/`.

## Verification gates — read `CLAUDE.md` before running these

**Never background a gate. Always foreground, with a generous explicit timeout.** A spawned
subagent receives no background-task completion notification and will stall forever waiting on a
signal that cannot arrive. This has now happened to several agents in this repo.

**Run from the REPO ROOT, never from `backend/`.** `testpaths` is defined in the root
`pyproject.toml` and `tests/` is a sibling of `backend/` — from `backend/` the run collects **zero**
tests and exits 0, which is indistinguishable from a pass at a glance.

Run each as its own foreground command and read its exit code directly:

1. `uv run pytest` (~3.5 minutes — set a generous timeout)
2. `uv run ruff check .`
3. `uv run ruff format --check .`

`ruff check` passing is **not** `ruff format --check` passing. They are separate gates. Report the
actual test counts (backend), not "tests pass."

## When done

1. Update this file's frontmatter: `status`, `completed`, `result`.
2. `git mv` this file into `prompts/done/` (success) or `prompts/failed/` (failure).
3. Record decisions in `docs/decisions.md` (see above).
4. **Do not commit.** Prepare the working tree and report back to the orchestrating session:
   the file list, a proposed one-line `feat:` message, the three gate exit codes, the test counts,
   and anything in the spec you found wrong or underspecified. The orchestrating session surfaces
   the `y/n`.

Note the commit will also carry `docs/download-client-framework-spec.md`, which is untracked input
to this task, and any `docs/decisions.md` edit. Never `git add -A`, never push, never auto-commit.
Branch is `dev` — never `main`.
