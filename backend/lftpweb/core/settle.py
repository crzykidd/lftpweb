"""The settle gate (`prompts/open-issues.md` "2 -- the settle gate"). DESIGN.md wording is
proposed for this but not yet applied -- see `docs/decisions.md` and this task's handoff
report; the doc gets corrected explicitly, not quietly diverged from.

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

**Settled** means this fingerprint held for `REQUIRED_SETTLE_SCANS` consecutive scans.
Persisted (migration 007, `item_settle`) rather than kept only in memory: it must survive a
restart, and -- decisively -- nothing may publish a state it did not read back from a table
(DESIGN.md's own invariant; see `core/itemview.py`'s module docstring). `core/engine.py`'s
`_persist` is the only writer; `core/autoqueue.py` (the queueing gate) and `core/queue.py`'s
`_reap_one` (the completion gate) are the two readers.

Every function here that doesn't need the database is a pure function, unit-tested without one
-- the same shape `core/reconcile.py` and `core/mount_sentinel.py.resolve_absence` use.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import aiosqlite

    from lftpweb.core.remote import RemoteEntry

# DESIGN.md open-issues #2: "unchanged across 2 consecutive scans." A named constant, not a
# bare 2, because it's a decision, not an accident -- and its wall-clock meaning changes once
# per-queue scan intervals land (prompts/open-issues.md #11): today every queue shares one
# `scan_interval_s` (default 30s, `core/engine.py`), so 2 scans is up to ~60s of added
# latency; once that interval is configurable per queue, "2 scans" will mean very different
# amounts of real time on a 10s-interval queue versus a 60s-interval one, for the identical
# setting.
REQUIRED_SETTLE_SCANS = 2

# (file_count, total_bytes, max_mtime) over one top-level item's whole remote subtree.
# `max_mtime` is `None` only when the item has no remote files under it at all yet (a bare,
# just-created directory).
Fingerprint = tuple[int, int, float | None]


@dataclass(frozen=True)
class SettleRecord:
    fingerprint: Fingerprint
    matched_scans: int


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
    prev: SettleRecord | None, fingerprint: Fingerprint, *, partial_scan: bool
) -> SettleRecord:
    """One scan's worth of settle-counter arithmetic for a single top-level item.

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
    counter at 1 exactly as a clean first scan would.

    Otherwise: an unchanged fingerprint increments the counter; a changed one resets it to 1 --
    a fresh sighting, because something about the remote subtree just moved.
    """
    if partial_scan and prev is not None:
        return prev
    if prev is not None and prev.fingerprint == fingerprint:
        return SettleRecord(fingerprint=fingerprint, matched_scans=prev.matched_scans + 1)
    return SettleRecord(fingerprint=fingerprint, matched_scans=1)


def is_settled(record: SettleRecord | None) -> bool:
    """`True` once `record` has held its fingerprint for `REQUIRED_SETTLE_SCANS` consecutive
    scans. `None` (never scanned) is never settled.
    """
    return record is not None and record.matched_scans >= REQUIRED_SETTLE_SCANS


# --- Persistence (migration 007, `item_settle`) -- `core/engine.py._persist` is the only
# writer; `core/autoqueue.py` / `core/queue.py._reap_one` read the table directly for their own
# narrower questions rather than going through `load_settle_records` (loading a whole queue's
# records for a single-item lookup would be wasted work on every job completion). -----------


async def load_settle_records(db: "aiosqlite.Connection", queue_id: int) -> dict[str, SettleRecord]:
    cursor = await db.execute(
        "SELECT rel_path, file_count, total_bytes, max_mtime, matched_scans "
        "FROM item_settle WHERE queue_id = ?",
        (queue_id,),
    )
    rows = await cursor.fetchall()
    return {
        row["rel_path"]: SettleRecord(
            fingerprint=(row["file_count"], row["total_bytes"], row["max_mtime"]),
            matched_scans=row["matched_scans"],
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
            VALUES (?, ?, ?, ?, ?, ?, STRFTIME('%Y-%m-%dT%H:%M:%fZ','now'))
            ON CONFLICT (queue_id, rel_path) DO UPDATE SET
                file_count = excluded.file_count,
                total_bytes = excluded.total_bytes,
                max_mtime = excluded.max_mtime,
                matched_scans = excluded.matched_scans,
                updated_at = excluded.updated_at
            """,
            (queue_id, rel_path, file_count, total_bytes, max_mtime, record.matched_scans),
        )


async def is_settled_in_db(db: "aiosqlite.Connection", queue_id: int, rel_path: str) -> bool:
    """Single-item settle check -- the shape `core/autoqueue.py` and
    `core/queue.py._reap_one` both need, without loading a whole queue's records for one
    lookup. Reflects the state as of the most recent scan; a job can finish mid scan-interval,
    so this is the best information available, not a live recomputation.
    """
    cursor = await db.execute(
        "SELECT matched_scans FROM item_settle WHERE queue_id = ? AND rel_path = ?",
        (queue_id, rel_path),
    )
    row = await cursor.fetchone()
    return row is not None and row["matched_scans"] >= REQUIRED_SETTLE_SCANS


# --- Settings (JSON in `setting`, the same pattern `core/postprocess.py.PostprocessSettings`
# uses) -------------------------------------------------------------------------------------

SETTING_KEY = "settle_settings"


@dataclass(frozen=True)
class SettleSettings:
    """Site-level toggle. **Defaults off** -- this project's rule is that a new capability
    ships off unless there's an explicit, reasoned exception (this task's own report has the
    full reasoning: the gate delays every transfer by up to
    `REQUIRED_SETTLE_SCANS * scan_interval_s`, today up to ~60s, including the atomic
    hardlink path where it buys nothing -- that is not the same bar `move`-mode delete safety
    or the phase 7 scheduled backup cleared to earn their exceptions).
    """

    enabled: bool = False


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
