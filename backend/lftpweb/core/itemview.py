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
#
# `downloaded_at`/`verified_at`/`extracted_at`/`first_missing_at`/`remote_deleted_at` (2026-08-13,
# prompts/2026-08-13-lifecycle-icons.md) feed the lifecycle facets below and their tooltips --
# see `_lifecycle_facets`'s own docstring for why the *raw* timestamps travel on the wire
# alongside the derived `level`/`reason` rather than a pre-formatted "2h ago" string: turning a
# timestamp into relative text is a presentation concern the frontend already owns for
# `state_changed_at` (`lib/format.ts`), and duplicating that here would be a second place doing
# the same job.
ITEM_VIEW_COLUMNS = (
    "id, rel_path, is_dir, remote_size, local_size, remote_mtime, state, substate, "
    "state_changed_at, downloaded_at, verified_at, extracted_at, first_missing_at, "
    "remote_deleted_at"
)

# The published shape is a plain dict on purpose: it *is* the JSON that goes on the wire and
# the kwargs `models.FileNode` takes, so there is no second representation to convert between
# (and nothing that can be serialized differently by one caller than another).
ItemView = dict[str, Any]

# --- Lifecycle facets (2026-08-13, prompts/2026-08-13-lifecycle-icons.md) --------------------
#
# `item.state` carries at least five orthogonal facts in one slot: whether a remote copy
# exists, whether a local copy exists, whether it was verified, whether it was extracted, and
# what is currently happening to it. That collapsing is the root cause this task's prompt
# names directly -- a `LOCAL_ONLY` reading clobbering a `move`-mode item's `EXTRACTED` outcome,
# `REMOVED_BOTH` overloaded to also mean "local gone, remote untouched," a `DOWNLOADED` row
# claiming bytes that are not on disk during §7.3's grace period. Below derives four small,
# independently-colored **facets** -- R(emote)/L(ocal)/V(erified)/E(xtracted) -- from the same
# persisted row, alongside `state`, never instead of it: every consumer of this projection
# keeps publishing `state` unchanged, and nothing here feeds back into how `state` itself is
# computed or transitions (`core/reconcile.py`, `core/engine.py._persist`,
# `core/mount_sentinel.py.resolve_absence`, `core/postprocess.py.outcome_survives_rescan` are
# untouched). This is a *display* projection of facts that are already fully persisted.
#
# **Presence vs. milestone -- the distinction that makes this correct, not just prettier**
# (recorded in docs/decisions.md; do not collapse it back):
#
# - R and L are **presence** facets: true *right now*, and may legitimately go dark. A `move`
#   -mode item's R goes dark the moment its verified remote copy is deleted on purpose --
#   that's the display being honest, not losing information.
# - V and E are **milestone** facets: they record that verification/extraction *happened*, read
#   from `verified_at`/`extracted_at` -- timestamp columns nothing ever clears -- never from
#   `item.state` alone. That's what makes them survive a rescan that changes `state` out from
#   under them (e.g. `EXTRACTED` -> `LOCAL_ONLY` structurally, `state` held at `EXTRACTED` by
#   `outcome_survives_rescan`): the milestone reads correctly either way, because it was never
#   derived from the state string in the first place.
#
# Each facet is `{"level": "green"|"amber"|"red"|"dim", "reason": <short code>}`. `level` is
# the only field a renderer needs for color; `reason` (plus the raw timestamp/size fields
# already on the row) is what a tooltip is built from -- composed in the frontend, the same way
# `lib/format.ts.stateAgeLabel` already turns a raw `state`/`state_changed_at` pair into a
# sentence, so there is exactly one place (here) that decides what a fact *means* and one place
# (the frontend) that turns it into words.

# States meaning "the download step is finished; every byte this item was ever going to get is
# either fully here or accounted for" -- `core/postprocess.py`'s own vocabulary for the same
# idea (`OWNED_STATES`, `{"DOWNLOADED"} | OWNED_STATES` re-exported as
# `core/mount_sentinel.py.COMPLETE_STATES`). **Deliberately duplicated, not imported**: this
# module has no dependencies of its own by design (it is the projection every other module
# reads back through), and `core/postprocess.py` already imports `item_view` from here, with
# `core/mount_sentinel.py` importing `core/postprocess.py` in turn -- importing either back
# would be circular. If a state is ever added to that set, add it here too; there is no way to
# make Python enforce the two staying in sync, only this comment.
_LOCAL_CONTENT_ASSERTED_STATES = frozenset(
    {"DOWNLOADED", "VERIFYING", "VERIFIED", "CORRUPT", "EXTRACTING", "EXTRACTED", "EXTRACT_FAILED"}
)

# `core/local_delete.py._removed_state_for` picks between these two at delete time; both mean
# "this codebase removed the local copy on purpose," so L is dark for either regardless of
# whatever `local_size` the row happens to still be carrying (see `_local_facet`'s docstring
# for why that column can't be trusted here).
_LOCAL_REMOVED_STATES = frozenset({"REMOVED_LOCAL", "REMOVED_BOTH"})


def _remote_facet(remote_size: int | None, remote_deleted_at: str | None) -> dict[str, str]:
    """R -- does a remote copy exist right now. A pure presence fact, so there is no "in
    progress" or "failed" reading for it (only L/V/E ever go amber or red): `remote_size IS NOT
    NULL` is the whole rule, matching every other reader of this column
    (`FileTree.tsx.hasRemoteCopy`, `core/local_delete.py._removed_state_for`).

    **Never red, even when absent.** The worked example this task is built around -- a
    completed `move`-mode item, verified then deleted on purpose -- has `remote_size IS NULL`
    on its very next scan (`core/engine.py._persist` always writes the fresh reconciled
    `remote_size`, even for a row whose `state` an outcome held steady) and would misread as a
    fault if presence alone drove color the way it does for a value that's actually supposed to
    stay put. `remote_deleted_at` (set only by `core/postprocess.py`'s move-mode delete, never
    by a manual local-only delete) is what lets the tooltip say "we removed it after verifying"
    instead of "it's just gone" -- distinct *reasons*, identical dim, per the task's own
    instruction that a successful move must never render as a failure.
    """
    if remote_size is not None:
        return {"level": "green", "reason": "present"}
    if remote_deleted_at is not None:
        return {"level": "dim", "reason": "deleted_by_us"}
    return {"level": "dim", "reason": "no_remote"}


def _local_facet(
    state: str,
    local_size: int | None,
    remote_size: int | None,
    first_missing_at: str | None,
) -> dict[str, str]:
    """L -- local presence, three-valued (absent/partial/complete -> dim/amber/green).

    **Leaf rule, reused rather than reinvented**: for a plain in-progress item
    (QUEUED/DOWNLOADING/PARTIAL/STOPPED/FAILED/REMOTE_ONLY, and directories in the same
    structural states -- `local_size`/`remote_size` are already rollup sums for a directory,
    `core/reconcile.py`, so no separate directory rule is needed here), completeness is exactly
    `local_size >= remote_size`, the same inequality `core/reconcile.py` uses to decide
    PARTIAL-vs-DOWNLOADED. `local_size` absent or `<= 0` reads absent regardless of
    `remote_size`; otherwise a `remote_size` this facet can't compare against still reads
    partial (something is there, but nothing proves it's everything) rather than guessing
    complete.

    **Every other branch below exists because that leaf rule gives the wrong answer for a
    state where "complete" isn't a byte comparison**:

    - `state` in `_LOCAL_CONTENT_ASSERTED_STATES` (DOWNLOADED and everything
      `core/postprocess.py` refines it into): the download step is over, so this reads complete
      -- *unless* `first_missing_at` is set, which only happens while §7.3's grace period is
      running (`core/mount_sentinel.py.resolve_absence` holds `state` at its last complete
      value while stamping this column the moment local content actually vanished). That one
      check is what makes a vacuously-`DOWNLOADED` directory (every child `EXCLUDED`, real
      remote bytes, zero local bytes by design -- `core/reconcile.py`'s "nothing left in them"
      branch) read complete/green while an `*arr`-imported-out `DOWNLOADED` item -- the exact
      case item 8 of this task's prompt asks to make visible -- reads dark, from the same
      `state` value. Also covers 4533617's case for free: an `EXTRACTED` item whose spent
      archive volumes were deleted after a successful extraction has `local_size < remote_size`
      by design, and this branch never looks at either size.
    - `state` in `_LOCAL_REMOVED_STATES` (REMOVED_LOCAL/REMOVED_BOTH): always dark. Not derived
      from `local_size`, deliberately -- `core/local_delete.py._mark_subtree_removed` updates
      only `state`/`auto_queue_suppressed`/`suppressed_reason` at delete time, so the row's
      `local_size` is **known-stale** (the pre-delete value) until the *next* scan corrects it,
      and the very moment this matters most is the `item_delta` published immediately after a
      manual delete, before any scan has run.
    - `state == "EXCLUDED"`: dark, but its own reason code -- "never going to arrive, on
      purpose" (§3.2 rule 8, §4.7) must not read the same as "missing." A leaf-only state
      (`core/reconcile.py` never produces it for a directory), so the vacuous-directory case
      above is what a whole folder of excluded files renders as, not this branch.
    - `state == "LOCAL_ONLY"`: green -- there is no `remote_size` to compare against
      (`remote_entry is None` is exactly why `core/reconcile.py` chose this state), and
      "presence" for a never-remotely-tracked file can only mean "all of it that exists is
      here."
    """
    if state in _LOCAL_CONTENT_ASSERTED_STATES:
        if first_missing_at is not None:
            return {"level": "dim", "reason": "missing"}
        return {"level": "green", "reason": "complete"}
    if state in _LOCAL_REMOVED_STATES:
        return {"level": "dim", "reason": "removed_by_us"}
    if state == "EXCLUDED":
        return {"level": "dim", "reason": "excluded"}
    if state == "LOCAL_ONLY":
        return {"level": "green", "reason": "local_only"}

    if local_size is None or local_size <= 0:
        return {"level": "dim", "reason": "absent"}
    if remote_size is not None and local_size >= remote_size:
        return {"level": "green", "reason": "complete"}
    return {"level": "amber", "reason": "partial"}


def _verified_facet(state: str, verified_at: str | None) -> dict[str, str]:
    """V -- verification, a milestone. `verified_at` (set only by
    `core/postprocess.py`'s VERIFIED branch, never cleared) wins over everything else and is
    checked first, which is what lets this stay green after a later rescan moves `state` on to
    EXTRACTING/EXTRACTED -- the milestone doesn't need `state` to still say VERIFIED to still be
    true. `CORRUPT` gets its own red reading because verification *did* run and did not set
    `verified_at` (`core/postprocess.py` writes `state='CORRUPT'` plus an `error_class`/
    `error_detail`, deliberately not a timestamp for a failed check); VERIFYING is the one
    in-flight state this facet reports on (unlike the activity states in step 5, this *is* part
    of the accumulated lifecycle the icons exist to show). Anything else -- never verified, or
    verification disabled for this queue -- is dim; this facet cannot and does not distinguish
    those two, since neither leaves any trace on the row.
    """
    if verified_at is not None:
        return {"level": "green", "reason": "verified"}
    if state == "CORRUPT":
        return {"level": "red", "reason": "corrupt"}
    if state == "VERIFYING":
        return {"level": "amber", "reason": "in_progress"}
    return {"level": "dim", "reason": "not_verified"}


def _extracted_facet(state: str, extracted_at: str | None) -> dict[str, str]:
    """E -- extraction, a milestone. Exactly `_verified_facet`'s shape, one step later in the
    pipeline: `extracted_at` (set only on `core/postprocess.py`'s EXTRACTED branch, never
    cleared) wins first, `EXTRACT_FAILED` reads red (extraction ran and did not produce a
    timestamp), `EXTRACTING` reads amber (in progress), anything else is dim -- including an
    item with nothing to extract in the first place, which this facet cannot tell apart from
    "extraction is enabled but hasn't run yet."
    """
    if extracted_at is not None:
        return {"level": "green", "reason": "extracted"}
    if state == "EXTRACT_FAILED":
        return {"level": "red", "reason": "failed"}
    if state == "EXTRACTING":
        return {"level": "amber", "reason": "in_progress"}
    return {"level": "dim", "reason": "not_extracted"}


def _lifecycle_facets(row: Mapping[str, Any]) -> dict[str, dict[str, str]]:
    """The four facets for one row, keyed the way the wire (and `models.LifecycleFacets`)
    expects. A thin composition over the four pure predicates above -- kept as its own function
    so a test can assert on the whole bundle at once (the "reaches all three projection
    consumers" requirement only needs one shared call site to hold for every one of them, and
    this is it).
    """
    return {
        "remote": _remote_facet(row["remote_size"], row["remote_deleted_at"]),
        "local": _local_facet(
            row["state"], row["local_size"], row["remote_size"], row["first_missing_at"]
        ),
        "verified": _verified_facet(row["state"], row["verified_at"]),
        "extracted": _extracted_facet(row["state"], row["extracted_at"]),
    }


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

    `state_changed_at` (migration 006) is when `state` last actually changed value, stamped by
    that migration's own triggers — nothing in this codebase writes it directly. `None` only
    for a row the migration's backfill genuinely couldn't date; the frontend must handle that.
    Passed through verbatim, like `substate`.

    `downloaded_at`/`verified_at`/`extracted_at`/`first_missing_at`/`remote_deleted_at`
    (2026-08-13, prompts/2026-08-13-lifecycle-icons.md) are passed through verbatim too — the
    same "no affinity mismatch to correct for" as `substate`/`state_changed_at` — for two
    reasons: a tooltip needs the exact instant a milestone was earned, not just its color, and
    `facets` below is derived from them, so a test can assert the derivation without a
    database.

    `facets` (`_lifecycle_facets`) is the one place R/L/V/E get computed, so `GET /api/files`,
    `queue_delta`, `item_delta`, and connect-time `snapshot()` cannot disagree about what a
    row's icons should show — they are all this same function.
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
        "state_changed_at": row["state_changed_at"],
        "downloaded_at": row["downloaded_at"],
        "verified_at": row["verified_at"],
        "extracted_at": row["extracted_at"],
        "first_missing_at": row["first_missing_at"],
        "remote_deleted_at": row["remote_deleted_at"],
        "facets": _lifecycle_facets(row),
    }
