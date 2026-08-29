"""The settle gate -- DESIGN.md §3.3, with the eligibility half in §4.7 and the completion half
in §6's trigger paragraph.

Built across two tasks (the original build, then `prompts/done/2026-08-12-settle-gate-
followups.md` for the stuck-item self-heal, the wall-clock floor below, and defaulting this on);
both tasks drafted DESIGN.md wording rather than applying it, and **both wordings have since
been applied** -- §3.3 is the settle gate's own section, and §6 now names the two
post-processing call sites rather than only the job-success one. `docs/decisions.md` carries the
reasoning and the rejected alternatives for each.

**The problem this exists to catch.** A seedbox may still be writing a top-level item
(DESIGN.md §4.7's granularity) when a scan observes it. `core/reconcile.py` decides
completeness by comparing remote-vs-local bytes, recomputed fresh every scan -- and that
comparison genuinely cannot tell a finished item from one still arriving one file at a time.
A single growing file self-heals (queued, downloads a prefix, re-queued, resumes -- wasteful,
not corrupting). A release directory does not: if the scan catches 3 of an eventual 8 files
and each of those 3 is individually whole, the rollup reads the *directory* as complete. Not a
boundary race -- the normal outcome of uploading a multi-file release. Post-processing then
runs on a half release.

**The fingerprint.** Per top-level item, over its whole remote subtree:
`(file_count, total_bytes, max_mtime)` -- deliberately not mtime alone (rsync/scp/torrent
clients routinely preserve or preset it, and a directory's own mtime only moves when an entry
is added or removed, never when a child grows in place) and not size alone (that is exactly
the directory bug above: a subset of files, each complete, sums to a size that never changes
again once no more bytes are pending for *those* files). All three together change on every
way an item can still be arriving: a new file appearing, an existing one growing, or the
newest write landing.

**Settled** means this fingerprint held for `REQUIRED_SETTLE_SCANS` consecutive scans **and**
for at least `SETTLE_MIN_AGE_S` of wall-clock time since it was first observed
(prompts/2026-08-12-settle-gate-followups.md item 2) -- a scan count alone is only a reliable
proxy for "quiet for a while" as long as every queue shares one scan interval; the moment a
queue can poll faster than another (per-queue scan interval, prompts/open-issues.md #11, not
yet built), "2 scans" stops meaning a fixed amount of real time. Persisted (migration 007,
`item_settle`) rather than kept only in memory: it must survive a restart, and -- decisively --
nothing may publish a state it did not read back from a table (DESIGN.md's own invariant; see
`core/itemview.py`'s module docstring). `core/engine.py`'s `_persist` is the only writer;
`core/autoqueue.py` (the queueing gate) and `core/queue.py`'s `_reap_one` (the completion
gate) are the two readers.

Every function here that doesn't need the database is a pure function, unit-tested without one
-- the same shape `core/reconcile.py` and `core/mount_sentinel.py.resolve_absence` use.
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from lftpweb.core.clients.models import TransferPhase

if TYPE_CHECKING:
    from collections.abc import Iterable

    import aiosqlite

    from lftpweb.core.clients.models import Transfer
    from lftpweb.core.remote import RemoteEntry

# DESIGN.md open-issues #2: "unchanged across 2 consecutive scans." A named constant, not a
# bare 2, because it's a decision, not an accident.
REQUIRED_SETTLE_SCANS = 2

# prompts/2026-08-12-settle-gate-followups.md item 2. `REQUIRED_SETTLE_SCANS` alone silently
# weakens the moment a queue's own scan interval can differ from another's (per-queue scan
# interval, prompts/open-issues.md #11, not yet built): 2 matching scans on a queue polled
# every 10s is a ~20s quiet window, not the ~60s this gate has always been described (and
# accepted) as costing (docs/decisions.md). 60.0, not "somewhere in 60-90s", because it is the
# exact number already on record everywhere in this project as what the gate costs at today's
# single 30s-interval default -- a 30s queue's worst case is unchanged by this floor (it
# already reached this bound), while a faster queue is brought up to the same guarantee rather
# than quietly given a weaker one merely because it polls more often.
#
# **Both conditions are independently load-bearing -- do not "simplify" this to one check.**
# The count alone cannot tell a fast-settling item from a slow-polling one that simply hasn't
# been re-scanned enough times yet; the age alone cannot tell "genuinely unchanged" from
# "haven't looked in a while" -- an item scanned once and then not rescanned for ten minutes
# because its queue got disabled is not settled merely because ten minutes elapsed with nobody
# checking. Only "N matches, spread over at least this much wall-clock time" means "watched and
# found stable."
SETTLE_MIN_AGE_S = 60.0

# (file_count, total_bytes, max_mtime) over one top-level item's whole remote subtree.
# `max_mtime` is `None` only when the item has no remote files under it at all yet (a bare,
# just-created directory).
Fingerprint = tuple[int, int, float | None]


@dataclass(frozen=True)
class SettleRecord:
    fingerprint: Fingerprint
    matched_scans: int
    # Wall-clock epoch seconds (`time.time()`-comparable) since the *current* matched streak
    # began -- i.e. since this exact fingerprint was first observed, whether that was a first
    # sighting or a reset after a change. Carried forward unchanged by every later matching or
    # held scan; only a fresh sighting or a changed fingerprint moves it forward. Persisted in
    # `item_settle.updated_at` (see the persistence section below) -- that column predates this
    # meaning and is reused rather than renamed via a migration this task does not need.
    first_matched_at: float
    # `first_observed_at`/`last_changed_at` (migration 013, 2026-08-13,
    # prompts/2026-08-13-settle-progress-visibility.md): what `matched_scans`/`first_matched_at`
    # alone can't answer -- how long this item has been watched at all, and when it last
    # actually moved, as opposed to "since the current streak began" (which resets on every
    # change and so can't tell a five-minute-old item apart from one that just started).
    #
    # `first_observed_at` is set once, the very first time this `(queue_id, rel_path)` is ever
    # observed, and carried forward unchanged by every later call regardless of match, hold, or
    # reset -- unlike `first_matched_at`, nothing ever moves it forward again.
    #
    # `last_changed_at` moves to `now` on the same calls that reset `matched_scans` (a fresh
    # sighting or a changed fingerprint) and is held on every other call (a match or a
    # partial-scan hold) -- see `advance_settle` below for exactly which branch does which.
    #
    # Both `None` for a `SettleRecord` built from a pre-migration-013 `item_settle` row that has
    # not been written since (`load_settle_records` below) -- there is no history to invent, and
    # the display renders `None` as "unknown" rather than a fabricated time. Also why both
    # default to `None` here rather than being required: `tests/test_settle.py` and
    # `tests/test_ws_deltas.py` construct `SettleRecord`s directly without them, and a record
    # missing this information is a real, expected state, not a test-only convenience.
    first_observed_at: float | None = None
    last_changed_at: float | None = None


def compute_fingerprints(remote_tree: Mapping[str, "RemoteEntry"]) -> dict[str, Fingerprint]:
    """One fingerprint per **top-level** `rel_path` in `remote_tree` (DESIGN.md §4.7's item
    granularity -- the same `instr(rel_path, '/') = 0` shape `core/autoqueue.py` already uses),
    computed over that item's whole remote subtree. Pure: takes the same root-relativized
    remote tree `core/reconcile.py.reconcile` does, does no I/O.

    Only **file** mtimes roll up into `max_mtime` -- a directory's own mtime is one of the two
    signals this module's docstring rules out, so it must not leak back in here. A top-level
    directory with no files under it yet gets `(0, 0, None)`.
    """
    file_count: dict[str, int] = {}
    total_bytes: dict[str, int] = {}
    max_mtime: dict[str, float | None] = {}

    for rel_path, entry in remote_tree.items():
        top = rel_path.split("/", 1)[0]
        if entry.is_dir:
            file_count.setdefault(top, 0)
            total_bytes.setdefault(top, 0)
            max_mtime.setdefault(top, None)
            continue
        file_count[top] = file_count.get(top, 0) + 1
        total_bytes[top] = total_bytes.get(top, 0) + entry.size
        prev = max_mtime.get(top)
        max_mtime[top] = entry.mtime if prev is None else max(prev, entry.mtime)

    return {top: (file_count[top], total_bytes[top], max_mtime[top]) for top in file_count}


def advance_settle(
    prev: SettleRecord | None, fingerprint: Fingerprint, *, partial_scan: bool, now: float
) -> SettleRecord:
    """One scan's worth of settle-counter arithmetic for a single top-level item. `now`
    (`time.time()`-comparable epoch seconds) is the caller's own scan-pass timestamp, injected
    rather than read here so this stays a pure function -- `core/engine.py._persist` is the one
    caller and already has a single `now` for the whole pass.

    **A scan carrying a partial-scan warning must never advance the counter.** GNU `find`
    exits nonzero the instant it can't stat/read one subdirectory anywhere in the tree, and
    still prints everything it *did* reach (`core/remote.py.interpret_primary_scan_result`).
    Two consecutive partial scans that happen to return the identical truncated subset would
    otherwise look like two matching fingerprints and read as settled -- exactly wrong, since
    the unreadable subtree is unaccounted for either way, not confirmed unchanged.

    **Held, not reset**, when `partial_scan` is true and there *is* a previous record: the
    conservative choice when there's no evidence anything actually changed, only that this
    pass couldn't see all of it. Resetting on every transient permission hiccup would mean an
    item under a directory with one routinely-unreadable sibling subtree never settles at all.
    A first sighting during a partial scan has no previous record to hold, so it starts the
    counter at 1 exactly as a clean first scan would. `first_matched_at` is held too -- a hold
    is "no evidence anything changed," so the streak's start must not appear to move just
    because this pass couldn't fully confirm it.

    Otherwise: an unchanged fingerprint increments the counter and **carries `first_matched_at`
    forward unchanged** (the streak began when it began, not when it was last reconfirmed); a
    changed one resets the counter to 1 and starts a fresh streak at `now` -- a fresh sighting,
    because something about the remote subtree just moved. **`matched_scans`'s own arithmetic is
    untouched by 2026-08-13's `prompts/2026-08-13-settle-progress-visibility.md`** -- an earlier
    version of that task tried starting it at `0` instead of `1` specifically for a change with a
    `prev` to compare against (to give the Files page a value distinct from a first-ever
    sighting), and `tests/test_settle_gate_e2e.py` caught why that was wrong before it shipped:
    it silently turned "2 consecutive unchanged scans" into 3 for every item that had ever
    changed once, which is exactly the growing-denominator problem this task's own brief already
    ruled out, just moved into the numerator instead of the denominator. Reverted; see
    `docs/decisions.md` for the full story. The Files page gets its "still changing" signal from
    `last_changed_at` below instead, at zero cost to `is_settled`'s timing.

    `first_observed_at` is carried forward from `prev` on every branch that has a `prev` at all
    (matched, held, or changed) and set to `now` only on a genuinely first-ever sighting --
    never moved once set. `last_changed_at` moves to `now` on exactly the two branches that
    reset `matched_scans` to 1 (a first sighting, or a differing fingerprint) and is held
    everywhere else (a match, or a partial-scan hold, both already returning early above) --
    this is the field that actually distinguishes "just detected still changing" from
    "confirmed unchanged once already," both of which read `matched_scans == 1`.
    """
    if partial_scan and prev is not None:
        return prev
    if prev is not None and prev.fingerprint == fingerprint:
        return SettleRecord(
            fingerprint=fingerprint,
            matched_scans=prev.matched_scans + 1,
            first_matched_at=prev.first_matched_at,
            first_observed_at=prev.first_observed_at,
            last_changed_at=prev.last_changed_at,
        )
    return SettleRecord(
        fingerprint=fingerprint,
        matched_scans=1,
        first_matched_at=now,
        first_observed_at=prev.first_observed_at if prev is not None else now,
        last_changed_at=now,
    )


def is_settled(record: SettleRecord | None, *, now: float | None = None) -> bool:
    """`True` once `record` has **both** held its fingerprint for `REQUIRED_SETTLE_SCANS`
    consecutive scans **and** the current streak (`first_matched_at`) is at least
    `SETTLE_MIN_AGE_S` old. `None` (never scanned) is never settled. `now` defaults to
    `time.time()`; injectable for tests, the same shape `core/progress.py.ProgressSampler.
    sample` uses.
    """
    if record is None or record.matched_scans < REQUIRED_SETTLE_SCANS:
        return False
    now = time.time() if now is None else now
    return (now - record.first_matched_at) >= SETTLE_MIN_AGE_S


# --- Persistence (migration 007, `item_settle`) -- `core/engine.py._persist` is the only
# writer; `core/autoqueue.py` / `core/queue.py._reap_one` read the table directly for their own
# narrower questions rather than going through `load_settle_records` (loading a whole queue's
# records for a single-item lookup would be wasted work on every job completion).
#
# `item_settle.updated_at` carries `SettleRecord.first_matched_at` (prompts/2026-08-12-settle-
# gate-followups.md item 2) -- **not** "the last time this row was written," which is what the
# column name suggests and what it meant before this task. No migration for this: the column
# already exists, nothing outside this module has ever read it (`load_settle_records`'s SELECT
# didn't even select it until this task), and `advance_settle` already computes the exact right
# value to store in it on every call -- carried forward on a match or a hold, reset to `now`
# only on a fresh sighting or a changed fingerprint. Renaming the column is left for a future
# migration that has an actual reason to touch this table again.
# ---------------------------------------------------------------------------------------------


def _parse_iso(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def _format_iso(value: float) -> str:
    return datetime.fromtimestamp(value, tz=UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _parse_iso_opt(value: str | None) -> float | None:
    """`_parse_iso`, but for `first_observed_at`/`last_changed_at` (migration 013), which are
    `NULL` on any `item_settle` row that predates that migration and hasn't been written since
    -- see `SettleRecord`'s own docstring for why `None` there is a real, expected state rather
    than something to paper over.
    """
    return None if value is None else _parse_iso(value)


def _format_iso_opt(value: float | None) -> str | None:
    return None if value is None else _format_iso(value)


async def load_settle_records(db: "aiosqlite.Connection", queue_id: int) -> dict[str, SettleRecord]:
    cursor = await db.execute(
        "SELECT rel_path, file_count, total_bytes, max_mtime, matched_scans, updated_at, "
        "first_observed_at, last_changed_at "
        "FROM item_settle WHERE queue_id = ?",
        (queue_id,),
    )
    rows = await cursor.fetchall()
    return {
        row["rel_path"]: SettleRecord(
            fingerprint=(row["file_count"], row["total_bytes"], row["max_mtime"]),
            matched_scans=row["matched_scans"],
            first_matched_at=_parse_iso(row["updated_at"]),
            first_observed_at=_parse_iso_opt(row["first_observed_at"]),
            last_changed_at=_parse_iso_opt(row["last_changed_at"]),
        )
        for row in rows
    }


async def save_settle_records(
    db: "aiosqlite.Connection", queue_id: int, records: Mapping[str, SettleRecord]
) -> None:
    """Upsert this scan's records. Does **not** commit -- `core/engine.py._persist` issues one
    commit for the whole scan pass, matching every other write in that method.

    `first_observed_at`/`last_changed_at` are written verbatim from the `SettleRecord`
    `advance_settle` already computed -- that function is what decides whether each one moves
    or holds for this call, so the upsert here has no arithmetic of its own to get wrong; it
    just persists what it was given, same as every other column on this row.
    """
    for rel_path, record in records.items():
        file_count, total_bytes, max_mtime = record.fingerprint
        await db.execute(
            """
            INSERT INTO item_settle
                (queue_id, rel_path, file_count, total_bytes, max_mtime, matched_scans, updated_at,
                 first_observed_at, last_changed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (queue_id, rel_path) DO UPDATE SET
                file_count = excluded.file_count,
                total_bytes = excluded.total_bytes,
                max_mtime = excluded.max_mtime,
                matched_scans = excluded.matched_scans,
                updated_at = excluded.updated_at,
                first_observed_at = excluded.first_observed_at,
                last_changed_at = excluded.last_changed_at
            """,
            (
                queue_id,
                rel_path,
                file_count,
                total_bytes,
                max_mtime,
                record.matched_scans,
                _format_iso(record.first_matched_at),
                _format_iso_opt(record.first_observed_at),
                _format_iso_opt(record.last_changed_at),
            ),
        )


@dataclass(frozen=True)
class SettleProgress:
    """A single item's own settle counters, read straight off `item_settle` -- the shape
    `core/autoqueue.py`'s Preflight projection needs (`core/preflight.py.PreflightRow.
    wait_scans`/`wait_since`) but `is_settled_in_db` below doesn't, since that function only
    ever needs a yes/no answer. `first_matched_at` is left as the raw ISO-8601 string the
    `updated_at` column already stores (`_format_iso`'s own output shape) -- no epoch round-
    trip here, since this record's one caller (the Preflight row) wants an ISO string for the
    wire, not a float to do arithmetic on.
    """

    matched_scans: int
    first_matched_at: str


async def settle_progress_in_db(
    db: "aiosqlite.Connection", queue_id: int, rel_path: str
) -> SettleProgress | None:
    """Single-row read of one item's own settle counters -- the same `item_settle` row
    `is_settled_in_db` below reads, factored out as its own function so a caller that wants the
    raw progress (not just the gate's yes/no) doesn't have to re-query. `None` when there is no
    row yet -- `is_settled_from_progress` already treats that as "not settled" (the gate's own
    conservative default); this mirrors that by giving the caller nothing to show rather than a
    fabricated zero.
    """
    cursor = await db.execute(
        "SELECT matched_scans, updated_at FROM item_settle WHERE queue_id = ? AND rel_path = ?",
        (queue_id, rel_path),
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    return SettleProgress(matched_scans=row["matched_scans"], first_matched_at=row["updated_at"])


def is_settled_from_progress(progress: SettleProgress | None, *, now: float | None = None) -> bool:
    """The gate's own yes/no check, over an already-read `SettleProgress` -- factored out of
    `is_settled_in_db` below so `core/autoqueue.py.on_scan` can read a row's progress exactly
    once per pass and get both the gate decision and the Preflight tooltip's own inputs from it,
    rather than querying twice. Checks both `REQUIRED_SETTLE_SCANS` and `SETTLE_MIN_AGE_S` -- see
    `is_settled`, which this mirrors for the DB-row shape rather than a `SettleRecord`.
    """
    if progress is None or progress.matched_scans < REQUIRED_SETTLE_SCANS:
        return False
    now = time.time() if now is None else now
    return (now - _parse_iso(progress.first_matched_at)) >= SETTLE_MIN_AGE_S


async def is_settled_in_db(
    db: "aiosqlite.Connection", queue_id: int, rel_path: str, *, now: float | None = None
) -> bool:
    """Single-item settle check -- the shape `core/queue.py._reap_one` needs, without loading a
    whole queue's records for one lookup. Reflects the state as of the most recent scan; a job
    can finish mid scan-interval, so this is the best information available, not a live
    recomputation. A thin wrapper over `settle_progress_in_db` + `is_settled_from_progress` above
    -- unchanged behavior, just factored so `core/autoqueue.py.on_scan` can reuse the read half
    on its own (see that function's own docstring for why).
    """
    progress = await settle_progress_in_db(db, queue_id, rel_path)
    return is_settled_from_progress(progress, now=now)


# --- Settings (JSON in `setting`, the same pattern `core/postprocess.py.PostprocessSettings`
# uses) -------------------------------------------------------------------------------------

SETTING_KEY = "settle_settings"


@dataclass(frozen=True)
class SettleSettings:
    """Site-level toggle. **Defaults on** as of `prompts/2026-08-12-settle-gate-followups.md`
    -- the third reasoned exception to this project's "every new capability ships off" rule,
    after `move`-mode verification and the phase 7 scheduled backup (`docs/decisions.md`
    records all three together). Shipped off originally (the settle-gate build task); flipped
    once the user, having read how it works, described it as how the system *should* behave
    and confirmed non-atomic remote copies are a real path on their setup -- at which point
    "off by default" stopped being the safe choice and became the wrong one: it is the fix
    for a real, confirmed-live directory-corruption bug (prompts/open-issues.md #2), and an
    existing install silently keeps running with that bug live unless this defaults on.

    Still switchable off (`GET`/`PUT /api/settings/settle`, Settings -> Transfer) for anyone
    whose seedbox landing path is atomic end to end and wants to shed the added latency
    entirely -- `REQUIRED_SETTLE_SCANS * SETTLE_MIN_AGE_S`'s floor, effectively `>=
    SETTLE_MIN_AGE_S` (60s today), on every transfer's completion, including the atomic
    hardlink path where nothing is actually still arriving. **Existing installs upgrading
    into this default will see transfers complete later than before** -- see CHANGELOG.md's
    `### Changed` entry, stated plainly rather than left for someone to notice.
    """

    enabled: bool = True

    # Stage 2b of #18 (docs/download-client-framework-spec.md §14, prompts/2026-08-23-settle-
    # gate-skip.md); reworked twice since -- 2026-08-24 (prompts/done/2026-08-24-client-
    # shortened-settle.md) and 2026-08-29 (prompts/done/2026-08-29-settle-verify-under-existing-
    # toggle.md, docs/decisions.md has the full history of both). **One toggle, one meaning:
    # turning this on means "verify a client's completion verdict by re-fingerprinting the
    # filesystem," never "trust the client's word."** When on, `core/autoqueue.py.on_scan`'s
    # client-shortened-settle mechanism (module docstring item 8) registers a pending recheck for
    # any eligible item a configured download client reports finished (`find_client_completion`
    # below) -- the background ticker then fingerprints the item's remote subtree, waits
    # `CLIENT_RECHECK_INTERVAL_S` (5s), fingerprints it again, and queues only if the two match.
    # A wrong or missing client verdict costs nothing under this mechanism: it never substitutes
    # for the ordinary settle gate's own fingerprint comparison, it only offers a faster path to
    # the identical "did anything move" answer.
    #
    # **Defaults `True`** -- a same-day reversal (2026-08-29) of the `False` this same flag
    # shipped with on 2026-08-23/24, and worth stating plainly rather than dressing up as if it
    # was always this way. The earlier `False` was earned by a real risk: back then, this flag
    # gated a *pure time-hold* (`CLIENT_COMPLETION_HOLD_S`/`client_completion_ready`, deleted
    # 2026-08-29) that trusted a terminal verdict once it was merely old enough, with no
    # re-fingerprint at all -- a wrong guess there meant transferring a half-written directory on
    # the strength of an unconfirmed status string (spec §13.4 guess #2). That risk is gone: the
    # flag now gates a mechanism that *verifies* on the filesystem before queuing anything, the
    # identical fingerprint comparison the ordinary settle gate already trusts, so there is no
    # longer a "wrong guess transfers something half-written" failure mode to default away from.
    # The user's own call, once that distinction was made explicit: *"yes, make it on by default
    # since it verifies."* The residual, narrower risk this default still carries -- candidate
    # selection runs through `find_client_completion`, which still depends on spec §13.4 guess #2
    # (a connector's history status mapping to `COMPLETED`/`SEEDING`) to decide which items are
    # even considered -- can only ever cost a missed opportunity to go faster (falls back to the
    # ordinary gate, which keeps running regardless), never a wrong "settled" verdict, so it does
    # not carry the same weight the old default-`False` reasoning did.
    #
    # **Nested under `enabled`, not independent of it.** `core/autoqueue.py.on_scan` only ever
    # consults this once the settle gate's own check has already decided an item is *not*
    # settled -- when `enabled` is `False` there is nothing to verify in the first place, so this
    # flag's value is inert either way, the same "off means untouched" relationship every other
    # settle-adjacent constant in this module already has to `enabled`. Still switchable off
    # (Settings -> Transfer) for anyone who would rather always wait out the full
    # `REQUIRED_SETTLE_SCANS`/`SETTLE_MIN_AGE_S` gate regardless of what a download client says.
    client_skip_enabled: bool = True


async def load_settle_settings(db: "aiosqlite.Connection") -> SettleSettings:
    cursor = await db.execute("SELECT value FROM setting WHERE key = ?", (SETTING_KEY,))
    row = await cursor.fetchone()
    if row is None:
        return SettleSettings()
    try:
        data = json.loads(row["value"])
    except (ValueError, TypeError):
        return SettleSettings()
    return SettleSettings(
        enabled=bool(data.get("enabled", False)),
        # `True` -- matching `SettleSettings.client_skip_enabled`'s own dataclass default above
        # (flipped 2026-08-29). This fallback fires when a stored blob predates this field
        # entirely (saved before 2026-08-23's stage 2b even existed) -- an install that has never
        # touched this setting should read the *current* default, not a value frozen from before
        # the field was introduced. A blob that already has the key (any install that has run
        # since 2026-08-23) is completely unaffected either way -- `.get` only ever falls back
        # when the key itself is absent, never when it's present and `False`.
        client_skip_enabled=bool(data.get("client_skip_enabled", True)),
    )


async def save_settle_settings(db: "aiosqlite.Connection", settings: SettleSettings) -> None:
    await db.execute(
        "INSERT INTO setting (key, value, updated_at) VALUES (?, ?, STRFTIME('%Y-%m-%dT%H:%M:%fZ','now')) "
        "ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (
            SETTING_KEY,
            json.dumps(
                {
                    "enabled": settings.enabled,
                    "client_skip_enabled": settings.client_skip_enabled,
                }
            ),
        ),
    )
    await db.commit()


# --- The client-verdict skip and withhold gate (stage 2b and stage 3 of #18) -----------------
#
# Pure, unit-tested without a database -- this module's own docstring's own rule, followed here
# exactly as `compute_fingerprints`/`advance_settle`/`is_settled` above already do. Neither
# function below ever talks to a client or a database; both take an already-fetched candidate
# list (`core/clientsync.py.ClientSyncScheduler.finished_transfers`/`failed_transfers`, spec
# §9.1's poller cache) and answer a yes/no question over it.

# 2026-08-24, prompts/2026-08-24-client-shortened-settle.md. `find_client_completion` below used
# to accept only `TransferPhase.COMPLETED` -- correct for SABnzbd (usenet has no seeding), but
# **structurally unreachable for an ordinary rTorrent torrent**: `core/clients/rtorrent.py.
# _classify_token` maps a finished, actively-seeding torrent to `SEEDING`, not `COMPLETED` --
# `complete and is_active` reads "seeding" -- and a torrent stops being active (and so reaches
# `COMPLETED`) only once it finishes seeding entirely, which for most setups is "never, until a
# ratio/time limit or a manual stop." The client-verdict skip therefore never fired for the
# ordinary case at all; `tests/test_clients_rtorrent.py`'s own
# `test_active_only_excludes_completed_but_keeps_seeding` documents that mapping as correct, and
# it stays correct here -- **the fix is what this gate accepts, not what the connector reports**
# (this task's own explicit rule: no `if client_type == "rtorrent"` branch anywhere in this
# subsystem, DESIGN.md §17 rule 6).
#
# `SEEDING` is not a queue-side status -- it is "finished and now uploading to peers," the
# terminal, done-with-all-its-own-work state for a torrent that will never itself transition to
# `COMPLETED` under ordinary operation. The existing rule this widens ("never let a queue-side
# status satisfy the gate," `find_client_completion`'s own docstring below) survives intact:
# `DOWNLOADING`, `QUEUED`, `PAUSED` and `FAILED` are still never a match, and -- decisively --
# `VERIFYING` (rTorrent's `hashing`, which overrides every other flag per `_classify_token`'s own
# total ordering) stays excluded too, because a torrent re-checking its data is not known-good
# regardless of what its `complete`/`is_active` flags said a moment before the recheck began.
#
# This widening changes nothing for SABnzbd (usenet has no seeding phase, so `SEEDING` never
# appears from that connector) and applies identically to the withhold gate's own self-lift check
# (`core/autoqueue.py.on_scan`, which calls `find_client_completion` on the same candidates before
# ever consulting `find_client_failure`) and to the pre-existing, still-off-by-default
# `client_skip_enabled` time-hold below -- both share this one function, so both get the fix
# together rather than needing two.
FINISHED_TRANSFER_PHASES = frozenset({TransferPhase.COMPLETED, TransferPhase.SEEDING})


def _client_content_path_matches(item_remote_path: str, content_path: str) -> bool:
    """Whether a download client's own reported `content_path` (spec §7.2: the only trustworthy
    identity source -- never a name heuristic, never a path predicted from a release name)
    refers to the same on-disk release as `item_remote_path` (a queue's own `remote_path` plus
    the item's top-level `rel_path` -- `core/autoqueue.py.on_scan`'s own join, matching
    `core/postprocess.py`/`core/arrsync.py`'s identical `remote_path.rstrip("/") + "/" +
    rel_path` convention).

    Equality, or `content_path` sitting at a *component boundary* under `item_remote_path` (the
    client reporting a specific file inside a multi-file release directory) -- **never a bare
    `str.startswith`**, which would let `/complete/ar-tv/Show.S01` spuriously swallow
    `/complete/ar-tv/Show.S01.EXTRA` (a sibling directory that merely shares a name prefix, the
    exact trap this task's own handoff prompt names). Both sides' trailing slashes are
    normalized away first -- the identical shape `core/arrsync.py._visible_path_contains` and
    `core/clients/rtorrent.py`'s own `free_space` matcher already use, for the identical reason.

    An empty `item_remote_path` (a queue with no `remote_path` configured, or an item with no
    `rel_path` -- neither should occur in practice, but this is a pure function with no
    caller-enforced precondition) never matches anything, rather than degenerating into "every
    path startswith the empty string."
    """
    root = item_remote_path.rstrip("/")
    if not root:
        return False
    candidate = content_path.rstrip("/")
    return candidate == root or candidate.startswith(root + "/")


def find_client_completion(
    item_remote_path: str, candidates: "Iterable[tuple[int, str, Transfer]]"
) -> "tuple[int, str, Transfer] | None":
    """The settle-gate skip's own yes/no lookup (stage 2b of #18) -- the first `candidates` entry
    whose `Transfer` is a **terminal, history-derived, finished verdict** (`FINISHED_TRANSFER_
    PHASES` above -- `COMPLETED` or `SEEDING`, never a queue-side status, and `TransferPhase.
    UNKNOWN` never satisfies anything either) with a `content_path` that matches
    `item_remote_path` (`_client_content_path_matches` above). `None` on no match, an empty
    `candidates` (an unreachable client, a blank queue/history response, or `client_skip_enabled`
    off upstream so the caller never gathered any) -- every one of those is "no information," and
    no information means the caller falls back to running the settle gate exactly as it runs
    today (this task's own non-negotiable).

    **Checks `transfer.phase in FINISHED_TRANSFER_PHASES` again here**, even though this
    function's real callers (`core/clientsync.py.ClientSyncScheduler.finished_transfers`) already
    filter to those phases before handing candidates over -- belt and suspenders, so this function
    is correct standing alone (and directly unit-testable against a non-finished `Transfer`
    without having to go through the scheduler's own cache at all), rather than depending on a
    caller never changing that filter.

    `candidates` carries `(instance_id, instance_name, transfer)` rather than a bare `Transfer`
    list so a caller that gets a match back has everything an audit event needs (naming the
    client instance and the verdict that permitted the skip, this task's own explicit
    requirement) without a second lookup.
    """
    for instance_id, instance_name, transfer in candidates:
        if transfer.phase not in FINISHED_TRANSFER_PHASES:
            continue
        if not transfer.content_path:
            continue  # absent is not a verdict (spec §4.2) -- no path, no match, ever
        if _client_content_path_matches(item_remote_path, transfer.content_path):
            return instance_id, instance_name, transfer
    return None


# 2026-08-29, prompts/done/2026-08-29-settle-verify-under-existing-toggle.md -- reworks the
# 2026-08-24 decision this constant and the two functions above it were part of
# (prompts/done/2026-08-24-client-shortened-settle.md), rather than extending it. That task
# shipped two mechanisms side by side: this re-fingerprint verify (then default OFF, with no
# toggle of its own) alongside `CLIENT_COMPLETION_HOLD_S`/`client_completion_ready` -- a pure
# time-hold that trusted a terminal verdict once it was merely old enough, with no re-fingerprint
# at all, gated by `SettleSettings.client_skip_enabled`. The user's own words: *"There is a
# toggle already for Skip the wait on a download client's own verdict. This setting should be the
# one that still does the 5s verify."* **One toggle, one meaning: skipping the wait means
# verifying that nothing moved.** `CLIENT_COMPLETION_HOLD_S` and `client_completion_ready` are
# deleted outright here, not kept as a degraded fallback -- `client_skip_enabled` now gates this
# re-fingerprint verify directly (`core/autoqueue.py.on_scan`'s own registration branch), so
# there is no code path left anywhere in this subsystem that queues on a download client's word
# alone. See `docs/decisions.md` for the rejected alternatives.
#
# `core/autoqueue.py`'s own client-shortened-settle mechanism (module docstring item 8)
# fingerprints the item's remote subtree (the identical `compute_fingerprints` this module
# already computes for the ordinary settle gate -- reused, not reinvented), waits
# `CLIENT_RECHECK_INTERVAL_S` seconds, and fingerprints it again -- **queuing only if the two
# fingerprints are equal**, exactly the ordinary settle gate's own stability test, just run on
# its own faster clock rather than waiting for `REQUIRED_SETTLE_SCANS` * `SETTLE_MIN_AGE_S`
# (>= 60s) of normal scan cadence. When the verify can't run at all -- no remote-scan capability
# wired, an unreachable host, a partial scan, or the item vanishing from the remote before the
# second fingerprint -- `AutoQueue.advance_pending_rechecks` drops the pending entry and the item
# falls back to the ordinary settle gate, never to trusting the client's verdict; there is no
# other fallback left to reach for now that `client_completion_ready` is gone.
#
# **Five seconds, not ten -- the user's own explicit instruction** for this task, overriding the
# 2026-08-24 pick of ten (which only justified itself as "the same number
# `CLIENT_COMPLETION_HOLD_S` already settled on," a constant that no longer exists to anchor it).
# The tradeoff is the identical one `REQUIRED_SETTLE_SCANS`/`SETTLE_MIN_AGE_S` balance -- waiting
# longer costs latency, waiting less risks a false "stable" reading on a release whose last bytes
# are still landing -- just drawn at a different point on it, because the prior this mechanism
# starts from is already strong: a client has already reported the release *terminal*, not merely
# quiescent, so a single confirming re-fingerprint five seconds later is still proportionate
# insurance, not a weakened check. Two fingerprints five seconds apart is the whole mechanism (not
# `REQUIRED_SETTLE_SCANS`-many) -- see `core/autoqueue.py.RECHECK_TICK_S` for why the tick that
# drives these two fetches had to shrink along with this number, not stay pinned at its old value.
CLIENT_RECHECK_INTERVAL_S = 5.0


def find_client_failure(
    item_remote_path: str, candidates: "Iterable[tuple[int, str, Transfer]]"
) -> "tuple[int, str, Transfer] | None":
    """The withhold gate's own yes/no lookup (stage 3 of #18, `docs/transfers-redesign-spec.md`
    §4.3, `core/autoqueue.py.on_scan`'s own third gate) -- `find_client_completion`'s mirror
    image, checking `TransferPhase.FAILED` instead of `COMPLETED`. The first `candidates` entry
    that is a **terminal, explicit `FAILED`** verdict with a `content_path` matching
    `item_remote_path` (`_client_content_path_matches` above, the identical component-boundary
    rule -- never a bare `str.startswith`).

    **Why this alone is already "explicit failure, not silence" (spec §4.2).** A client failing a
    release outright -- nothing ever landed -- has no on-disk path to report, so its history
    entry's `content_path` is empty and the `continue` below rejects it before matching is even
    attempted; "a client failing outright needs no code" (`docs/transfers-redesign-spec.md` §4.3)
    holds by construction here, the same way it does for a `COMPLETED` verdict with no path. Only
    a **partial** failure -- the download died partway, or unpack failed, leaving a half-written
    directory the client can still name -- ever reaches a real match, which is exactly the single
    case spec §4.3 says needs an explicit withhold at all.

    `None` on no match, or an empty `candidates` (an unreachable client, a blank queue/history
    response, `WithholdSettings.enabled` off upstream, or `AutoQueue.client_sync` never wired) --
    every one of those is "no information," and no information means auto-queue proceeds exactly
    as it does today (spec §4.2's "absent is not a verdict," restated for this gate).

    **Does not itself implement the self-lift rule** -- that is `AutoQueue.on_scan`'s own
    responsibility (checking `find_client_completion` first, on the same candidates set, and only
    consulting this function when that returns `None`), so a later genuine success always wins
    over a stale `FAILED` entry the client's own history may still be holding from a dead earlier
    attempt. Kept out of this function because it has no opinion about *why* it was called --
    the caller's own ordering is what makes the whole gate self-lifting, not anything this pure
    lookup could enforce on its own.
    """
    for instance_id, instance_name, transfer in candidates:
        if transfer.phase is not TransferPhase.FAILED:
            continue
        if not transfer.content_path:
            continue  # absent is not a verdict (spec §4.2) -- no path, no match, ever
        if _client_content_path_matches(item_remote_path, transfer.content_path):
            return instance_id, instance_name, transfer
    return None
