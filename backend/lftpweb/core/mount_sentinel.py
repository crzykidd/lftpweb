"""The mount / sentinel gate and the local-absence grace period (DESIGN.md §7.3).

**Required starting phase 4, not deferred with `sync`** — see docs/decisions.md. DESIGN.md
§7.3 writes these up as rails on delete propagation, which is unscheduled (`sync`) or phase 5
(`move`). But auto-queue (this phase) is the first feature that takes *action* — queueing a
transfer — on local absence, and a dropped NFS mount makes every tracked item look locally
absent in the very same scan. Both failure directions are destructive:

- items read `REMOTE_ONLY` ⇒ auto-queue re-downloads the entire library off one blip;
- items read `REMOVED_LOCAL` ⇒ auto-queue permanently skips them (once that detection exists,
  which this module also provides — see `resolve_absence`).

Two independent mechanisms:

1. **The sentinel gate (`check`/`write_if_needed`).** `core/engine.py` writes
   `.lftpweb-mount-ok` at a queue's local root after every scan that finds the root present,
   readable, and writable. `core/autoqueue.py` refuses to act on *anything* for a queue
   whose root fails `check()` — not deferred item-by-item, the whole queue's auto-queue pass
   is skipped, surfaced in the log (and, via `AutoQueue.gated`, the API/UI).
2. **The grace period + `REMOVED_LOCAL` transition (`resolve_absence`).** DESIGN.md §3.2 rule
   3: an item that was `DOWNLOADED` and is now locally absent (remote still present) becomes
   `REMOVED_LOCAL`, not a fresh `REMOTE_ONLY` — otherwise auto-queue would cheerfully
   re-fetch something the user (or an *arr import) deliberately removed. Absence must persist
   across several consecutive scans (default ~10 minutes) before the transition sticks, so a
   momentary NFS hiccup or an import-in-progress can't trigger it. `resolve_absence` is a pure
   decision function — `core/engine.py._persist` is the only I/O around it (reading/writing
   `item.state`/`item.first_missing_at`) — so the state machine is unit-testable without a
   filesystem or a database.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

SENTINEL_NAME = ".lftpweb-mount-ok"

# ~10 minutes (DESIGN.md §7.3's own default). Not user-configurable this phase — see
# docs/decisions.md; a future Settings knob can read from here without changing the
# function's shape.
DEFAULT_GRACE_S = 600.0

# States for which a fresh REMOTE_ONLY reading is worth reconsidering against history —
# `DOWNLOADED` is the state the grace clock can start from; `REMOVED_LOCAL` is where it's
# already landed and must stay landed until the item's local copy genuinely reappears.
_STICKY_PREV_STATES = frozenset({"DOWNLOADED", "REMOVED_LOCAL"})


def check(local_path: str) -> bool:
    """True iff `local_path` exists, is a readable+listable directory, and holds the
    sentinel. The nastiest case DESIGN.md §14 calls out by name — root exists, is readable,
    is empty, has no sentinel — reads `False` here, correctly: an empty directory and an
    unmounted share are indistinguishable by content alone, which is the whole reason the
    sentinel exists.
    """
    root = Path(local_path)
    try:
        if not root.is_dir():
            return False
        if not os.access(root, os.R_OK | os.X_OK):
            return False
        return (root / SENTINEL_NAME).is_file()
    except OSError:
        return False


def write_if_needed(local_path: str) -> None:
    """Write the sentinel after a scan finds the local root present, readable, and writable.
    Idempotent — a no-op once the sentinel exists. Deliberately does **not** create
    `local_path` itself: a not-yet-mounted root must never earn trust just because we tried
    to write into it, since `mkdir` would happily create it on whatever filesystem the mount
    point currently resolves to (which is exactly the failure this file exists to catch).
    """
    root = Path(local_path)
    try:
        if not root.is_dir() or not os.access(root, os.W_OK | os.X_OK):
            return
        sentinel = root / SENTINEL_NAME
        if sentinel.is_file():
            return
        sentinel.write_text(
            "lftpweb was here — do not delete (DESIGN.md §7.3's mount sentinel).\n"
            f"Written {datetime.now(UTC).isoformat()}\n"
        )
        logger.info("wrote mount sentinel at %s", sentinel)
    except OSError as exc:
        logger.warning("could not write mount sentinel at %s: %s", local_path, exc)


def resolve_absence(
    *,
    prev_state: str | None,
    prev_first_missing_at: str | None,
    structural_state: str,
    mount_ok: bool,
    now: datetime,
    grace_s: float = DEFAULT_GRACE_S,
) -> tuple[str, str | None] | None:
    """Decide whether a fresh `REMOTE_ONLY` reading from `core/reconcile.py` should instead
    be persisted as `DOWNLOADED` (grace period still running, or the mount gate refuses to
    even start the clock) or `REMOVED_LOCAL` (grace period elapsed — DESIGN.md §3.2 rule 3).

    Returns `None` when the fresh structural state should be trusted as-is — including every
    case where the item was never `DOWNLOADED`/`REMOVED_LOCAL` in the first place, and the
    case where local presence has returned (the caller's own `structural_state` already
    reflects that as `PARTIAL`/`DOWNLOADED`, and clears `first_missing_at` by simply not
    carrying it forward). Otherwise returns `(state, first_missing_at)` to persist instead.

    `prev_state == 'REMOVED_LOCAL'` is sticky regardless of `mount_ok` — the mount gate's job
    is to keep a dropped mount from *starting* this transition, not to undo one that was
    already correctly made while the mount was healthy.
    """
    if structural_state != "REMOTE_ONLY" or prev_state not in _STICKY_PREV_STATES:
        return None

    if prev_state == "REMOVED_LOCAL":
        return ("REMOVED_LOCAL", prev_first_missing_at)

    # prev_state == "DOWNLOADED": a fresh transition candidate.
    if not mount_ok:
        # The mount gate: never start the grace clock on a reading we can't trust to mean
        # what it appears to mean. Keep showing the last-known-good state.
        return ("DOWNLOADED", prev_first_missing_at)

    if prev_first_missing_at is None:
        return ("DOWNLOADED", now.strftime("%Y-%m-%dT%H:%M:%S.%fZ"))

    first_missing = datetime.fromisoformat(prev_first_missing_at.replace("Z", "+00:00"))
    elapsed = (now - first_missing).total_seconds()
    if elapsed >= grace_s:
        return ("REMOVED_LOCAL", prev_first_missing_at)
    return ("DOWNLOADED", prev_first_missing_at)
