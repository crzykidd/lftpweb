"""The settle gate (`prompts/open-issues.md` "2 -- the settle gate"). DESIGN.md wording has
been drafted for this across two tasks -- the original build and
`prompts/2026-08-12-settle-gate-followups.md` (the stuck-item fix, the wall-clock floor below,
and defaulting this on) -- see `docs/decisions.md` for both; the doc gets corrected explicitly,
not quietly diverged from, and the second task's entry supersedes part of the first's already-
applied §6 wording (the post-processing trigger paragraph).

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

if TYPE_CHECKING:
    import aiosqlite

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
    because something about the remote subtree just moved.
    """
    if partial_scan and prev is not None:
        return prev
    if prev is not None and prev.fingerprint == fingerprint:
        return SettleRecord(
            fingerprint=fingerprint,
            matched_scans=prev.matched_scans + 1,
            first_matched_at=prev.first_matched_at,
        )
    return SettleRecord(fingerprint=fingerprint, matched_scans=1, first_matched_at=now)


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


async def load_settle_records(db: "aiosqlite.Connection", queue_id: int) -> dict[str, SettleRecord]:
    cursor = await db.execute(
        "SELECT rel_path, file_count, total_bytes, max_mtime, matched_scans, updated_at "
        "FROM item_settle WHERE queue_id = ?",
        (queue_id,),
    )
    rows = await cursor.fetchall()
    return {
        row["rel_path"]: SettleRecord(
            fingerprint=(row["file_count"], row["total_bytes"], row["max_mtime"]),
            matched_scans=row["matched_scans"],
            first_matched_at=_parse_iso(row["updated_at"]),
        )
        for row in rows
    }


async def save_settle_records(
    db: "aiosqlite.Connection", queue_id: int, records: Mapping[str, SettleRecord]
) -> None:
    """Upsert this scan's records. Does **not** commit -- `core/engine.py._persist` issues one
    commit for the whole scan pass, matching every other write in that method.
    """
    for rel_path, record in records.items():
        file_count, total_bytes, max_mtime = record.fingerprint
        await db.execute(
            """
            INSERT INTO item_settle
                (queue_id, rel_path, file_count, total_bytes, max_mtime, matched_scans, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (queue_id, rel_path) DO UPDATE SET
                file_count = excluded.file_count,
                total_bytes = excluded.total_bytes,
                max_mtime = excluded.max_mtime,
                matched_scans = excluded.matched_scans,
                updated_at = excluded.updated_at
            """,
            (
                queue_id,
                rel_path,
                file_count,
                total_bytes,
                max_mtime,
                record.matched_scans,
                _format_iso(record.first_matched_at),
            ),
        )


async def is_settled_in_db(
    db: "aiosqlite.Connection", queue_id: int, rel_path: str, *, now: float | None = None
) -> bool:
    """Single-item settle check -- the shape `core/autoqueue.py` and
    `core/queue.py._reap_one` both need, without loading a whole queue's records for one
    lookup. Reflects the state as of the most recent scan; a job can finish mid scan-interval,
    so this is the best information available, not a live recomputation. Checks both
    `REQUIRED_SETTLE_SCANS` and `SETTLE_MIN_AGE_S` -- see `is_settled`, which this mirrors for
    the DB-row shape rather than a `SettleRecord`.
    """
    cursor = await db.execute(
        "SELECT matched_scans, updated_at FROM item_settle WHERE queue_id = ? AND rel_path = ?",
        (queue_id, rel_path),
    )
    row = await cursor.fetchone()
    if row is None or row["matched_scans"] < REQUIRED_SETTLE_SCANS:
        return False
    now = time.time() if now is None else now
    return (now - _parse_iso(row["updated_at"])) >= SETTLE_MIN_AGE_S


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


async def load_settle_settings(db: "aiosqlite.Connection") -> SettleSettings:
    cursor = await db.execute("SELECT value FROM setting WHERE key = ?", (SETTING_KEY,))
    row = await cursor.fetchone()
    if row is None:
        return SettleSettings()
    try:
        data = json.loads(row["value"])
    except (ValueError, TypeError):
        return SettleSettings()
    return SettleSettings(enabled=bool(data.get("enabled", False)))


async def save_settle_settings(db: "aiosqlite.Connection", settings: SettleSettings) -> None:
    await db.execute(
        "INSERT INTO setting (key, value, updated_at) VALUES (?, ?, STRFTIME('%Y-%m-%dT%H:%M:%fZ','now')) "
        "ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (SETTING_KEY, json.dumps({"enabled": settings.enabled})),
    )
    await db.commit()
