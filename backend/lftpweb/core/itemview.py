"""The one projection of an `item` row into the shape every consumer sees (DESIGN.md §2, §9).

**The `item` table is the single authority for an item's state; the in-memory model is a
cache *of* it, and nothing publishes a value it did not read back.** Three modules write
`item.state` -- `core/queue.py` (job lifecycle), `core/postprocess.py` (§6's six states) and
`core/engine.py._persist` (the structural reading from `core/reconcile.py`, arbitrated
against both) -- and this module is the single read-back path they all publish through:
`core/engine.py`'s `queue_snapshot`/`queue_delta`, `core/queue.py`'s and
`core/postprocess.py`'s `item_delta`, and `GET /api/files`. One code path decides what an
item looks like whether it leaves over the socket or over HTTP.

**Why this exists at all.** Until it did, `core/engine.py` published `core/reconcile.py`'s
*structural* reading (REMOTE_ONLY/PARTIAL/DOWNLOADED, recomputed from remote-vs-local bytes
on every pass) while `_persist` wrote a possibly different state to the database — so the two
disagreed for every row `_persist` overrode. A `REMOVED_LOCAL` item, or one held `DOWNLOADED`
through §7.3's grace window, was published as `REMOTE_ONLY` — Queue button and all — from
phase 4 onwards, and `GET /api/files` (which has read the database since phase 3) disagreed
with the socket about the same item. Two places computing the same thing, kept in agreement
by remembering to, is what made that possible; there is now one place.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

# The columns `item_view` needs, as they appear in a SELECT. Kept next to the projection so
# the query and the projection can't drift apart -- adding a field to the wire means adding it
# in exactly one file. Callers that already hold a `SELECT *` row (`core/queue.py`,
# `core/postprocess.py`) just pass it straight in.
ITEM_VIEW_COLUMNS = "id, rel_path, is_dir, remote_size, local_size, remote_mtime, state, substate"

# The published shape is a plain dict on purpose: it *is* the JSON that goes on the wire and
# the kwargs `models.FileNode` takes, so there is no second representation to convert between
# (and nothing that can be serialized differently by one caller than another).
ItemView = dict[str, Any]


def item_view(row: Mapping[str, Any]) -> ItemView:
    """One persisted `item` row as the WebSocket and `GET /api/files` send it.

    `id` matters more than it looks: every action the Files page offers (Queue, Stop, the
    bulk operations) addresses an item by its `item.id`, and the page renders purely from the
    WebSocket stream. When the engine's serializer omitted it — as it did until it was caught
    against a real deployment — every row arrived with `id == null` and the UI silently
    rendered no action button at all, on every row, forever. Reading the projection out of the
    `item` table rather than out of `core/reconcile.py`'s output means the id can no longer go
    missing: it is the row's own primary key.

    Two conversions, both because SQLite's column affinities are not the wire's types:
    `is_dir` is stored as 0/1 (the wire wants a bool), and `remote_mtime` lives in a
    TEXT-affinity column, so a float written in comes back out as a string.

    `rel_path` needs no `core/util.py.to_safe_text` treatment here — a row can only have got
    into the table through `core/engine.py._persist`, which applies it on the way in (and a
    string carrying a lone surrogate could not be written to a TEXT column at all).

    `substate` (migration 007, `core/settle.py`) is `'settling'` for a top-level item held at
    `REMOTE_ONLY` by the settle gate, `None` otherwise — see that module's docstring. Passed
    through verbatim; unlike `remote_mtime` it has no affinity mismatch to correct for.
    """
    return {
        "id": row["id"],
        "rel_path": row["rel_path"],
        "is_dir": bool(row["is_dir"]),
        "state": row["state"],
        "substate": row["substate"],
        "remote_size": row["remote_size"],
        "local_size": row["local_size"],
        "remote_mtime": float(row["remote_mtime"]) if row["remote_mtime"] is not None else None,
    }
