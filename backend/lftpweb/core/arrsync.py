"""The *arr sync poller (docs/arr-integration-spec.md "The poller") -- background loop, same
`_task`/`start()`/`stop()` shape as `core/backup.py.BackupScheduler`, matching a bound Sonarr/
Radarr instance's `/api/v3/queue` against local items, and watching for import (or removal).

**Not wired into the scan pass** -- scan cadence is per-queue and variable (DESIGN.md §5), and
*arr polling wants its own clock, independent of it (spec: "not wired into the scan pass").

**Phase A** (`prompts/done/2026-08-15-arr-integration-backend.md`) built matching
(`(no status) -> detected`) and import/removal detection (`detected/notified -> imported/gone`).
**Phase B** (`prompts/2026-08-15-arr-integration-notify-cleanup.md`, this module now) adds the
two active behaviors on top: a **bounded retry** for a notify push whose primary attempt (fired
from `core/postprocess.py.PostprocessPipeline`'s own tail, per the spec's "Notify" section) has
already failed once (`_maybe_retry_notify` -- the actual POST and event-writing live in
`core/arrnotify.py.notify_arr`, shared by both callers so there is exactly one implementation),
and **cleanup** (`_maybe_cleanup`) for an `imported` item on an `arr_delete_completed` queue.

**Rung 4 of the move-mode delete ladder** (`_maybe_delete_remote_on_import`, added 2026-08-16,
`prompts/done/2026-08-16-move-delete-gate-ladder.md`, resolving open issue #2 /
`docs/audit-v0.1.0.md` G1): `core/postprocess.py._maybe_delete_remote` defers a `move`-mode
item's remote delete here, rather than performing it, whenever the item is *arr-tracked
(`arr_status` non-null) by the time its own pipeline run reaches the delete gate -- recorded in
`item.remote_delete_pending`. This module performs the deferred delete (via
`perform_remote_delete`, the one implementation, never a second one) the moment `_commit_terminal`
confirms `imported`, and *before* this poller pass's own `arr_delete_completed` cleanup sweep --
so "import green -> delete source -> (optionally) delete local" holds even within a single pass.
Never on `gone`. `remote_pool`/`host_provider` are optional, plain-attribute-after-construction
seams (like `in_flight_provider`/`delete_in_flight` below) -- `None` (a test fixture that doesn't
wire them) simply leaves a deferred item deferred for a later pass, the same "no-op until wired"
shape `_maybe_notify_arr` uses for a missing `config_dir`.

**The two-consecutive-passes quiescence guard is in-memory, not persisted** (deliberately: the
spec's "Data model" section specifies exactly three new `item` columns and no new table for this
feature, unlike `core/settle.py`'s `item_settle`). A restart loses any pending candidacy and
simply costs one extra poll interval before a transition commits -- safe, since "wait longer
before the irreversible step" is the direction restart-loss is allowed to err in. The notify
retry's own bounded-attempt counter (`_notify_attempts`) is in-memory for the identical reason;
losing it on restart only means a slow-to-notify item gets a few more attempts than the bound
technically allows, never fewer. See `_PendingVerdict` below.

**The rung-4 delete retries; it is no longer one-shot** (2026-08-17,
`prompts/done/2026-08-17-stranded-source-delete-retry.md`, live on both the user's test and
production systems: `SSH connection closed` on the deferred delete, `arr_cleanup` removing the
local copy anyway seconds later, and the resulting `REMOVED_LOCAL` row -- remote copy alive --
had no Delete affordance in the UI at all). Before this, the delete only ever fired once, from
`_commit_terminal`'s own `imported` transition; a transient SSH failure there stranded the
remote copy **permanently**, because nothing ever asked again. `_sweep_stranded_source_deletes`
now runs every pass, keyed off the debt itself (`item.remote_delete_pending IS NOT NULL`, a
terminal-import `arr_status`, `remote_deleted_at IS NULL`) rather than the transition that first
created it -- which is also what makes it a retroactive self-heal for a row already stranded
before this shipped, with no migration: the query alone matches it. Retries back off
per item (`_SourceDeleteRetryState`, the same growing-delay shape `_InstanceBackoff` above uses)
and pause after `MAX_SOURCE_DELETE_RETRY_ATTEMPTS`, writing one `remote_delete_retries_paused`
event rather than a `remote_delete_failed` every pass for as long as a seedbox stays down --
`remote_delete_pending` stays set throughout, so the manual Files-page delete (widened the same
day, see `frontend/src/lib/fileTree.ts.canDeleteLocal`) or a restart's clean in-memory slate can
still clear it. `_maybe_cleanup` also now withholds while a source delete is still owed
(`item.remote_delete_pending` non-null), restoring "delete source -> delete local" as an
enforced ladder order rather than a hoped-for one -- before this, cleanup ran regardless and the
local copy could vanish while the remote copy was still stranded, exactly what the production
incident above shows. And `_commit_terminal`'s `gone` branch now names a still-pending source
delete in its own event message (rung 4 never fires on `gone`, by design, unchanged) purely so
History can say why a source is still on the seedbox, without changing any behavior.

**Cleanup deliberately never writes `item.state` directly.** Unlike a manual Files-page delete
(`core/local_delete.py.delete_local`, which sets `REMOVED_LOCAL`/`REMOVED_BOTH` immediately,
because a human just confirmed the action), `_maybe_cleanup` removes the bytes and leaves
`item.state` exactly as it was -- the same pattern `core/postprocess.py._do_move` already
established for a staging relocation ("the next scan finds the item's local copy gone...and
[mount_sentinel's] REMOVED_LOCAL grace-period machinery takes it from there, the same as any
other externally-caused move"). This is what makes the spec's own UX description literally true
("downloaded -> processed -> (countdown) -> gone") rather than aspirational: the existing
`first_missing_at`/`REMOVAL_GRACE_ELIGIBLE_STATES` countdown chip
(`frontend/src/lib/format.ts`) only ever renders while a row is *not yet* `REMOVED_LOCAL` --
`delete_local`'s own immediate write would skip straight past that window and the chip would
never appear. See `docs/decisions.md` (2026-08-15) for the full reasoning and why this reads
"the existing local-deletion machinery" narrowly (its resolvers and guards, not its
state-writing tail).

**`dropped` -- an amber grace state between the two-pass quiescence guard and `gone`**
(2026-08-18, production incident, support bundle `lftpweb-support-0.2.3-20260818T013532Z`).
SABnzbd sometimes returns a blank/empty queue to Sonarr's own poll, so Sonarr's queue view
empties for a beat and the records return on the next refresh -- but this codebase's own poller
runs once a minute, slower than SABnzbd's blip, so *both* of the two-pass guard's observations
landed inside the same blank window: 8 items committed straight to `gone` in a single pass while
lftpweb was still actively downloading them (proof it was a blip, not a real removal -- their
verify/rename events ran minutes later). `gone` is deliberately terminal (`_REMATCHABLE_STATES`
below refuses to re-match a `gone`/`cleaned` row against the *identical* `downloadId` -- see that
set's own docstring and `docs/decisions.md`), and the stranded-source-delete sweep only ever
retries `imported`/`cleaned` rows, so all 8 rows sat with a permanent red dot, a parked rung-4
source delete, and no cleanup, even though the *arr imported every one of them normally an hour
later. `_check_import` below now commits `dropped` instead of `gone` at the point the two-pass
guard would have confirmed "no import evidence" -- everything from there is re-checked *every
subsequent pass* (`_check_dropped_items`), not gated behind another two-pass observation, since
`dropped` itself is already the "held for confirmation" state: the *same* `downloadId`
reappearing in the queue is direct evidence the disappearance was transient and sends the row
straight back to `detected` (`_match_items` widens its rematch candidates for `dropped` alone,
without `gone`/`cleaned`'s different-`downloadId` restriction -- see `docs/decisions.md`,
2026-08-18, for why the two states diverge here); an import history event promotes it to
`imported` through the normal `_commit_terminal` path (rung-4 delete + cleanup then proceed
exactly as any other import); and only once `arr_status_at` is older than `DROPPED_GONE_GRACE_S`
(6h, a deliberate constant -- see `docs/concepts.md`) with neither signal does it finally commit
`gone`. `_heal_stranded_gone_rows` is the retroactive, bounded counterpart for a row that already
committed `gone` before this shipped (the production 8, and any like them): keyed off
`arr_status='gone' AND remote_delete_pending IS NOT NULL AND remote_deleted_at IS NULL` -- the
debt itself, the same "query alone is the self-heal, no migration" shape
`_sweep_stranded_source_deletes` already established -- it re-asks `import_events` by the item's
own stored `arr_download_id` and promotes to `imported` the moment one shows up, bounded by
`MAX_GONE_HEAL_ATTEMPTS` (reusing `_InstanceBackoff`'s growing-delay shape, `_GoneHealRetryState`
below) so a genuinely-gone row doesn't get queried forever.
"""

from __future__ import annotations

import asyncio
import json
import logging
import posixpath
import re
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import aiosqlite

from lftpweb.core import audit, extract, mount_sentinel
from lftpweb.core.arrclient import (
    PAGE_SIZE,
    TRACKED_DOWNLOAD_STATE_IMPORTED,
    ArrClient,
    ArrClientError,
    HistoryEvent,
    QueueRecord,
    command_outcome,
)
from lftpweb.core.arrnotify import notify_arr, translate_to_arr_namespace
from lftpweb.core.crypto import DecryptionError, decrypt_secret
from lftpweb.core.events import EventBus
from lftpweb.core.itemview import item_view
from lftpweb.core.local_delete import DeleteInFlight, _do_remove_from_disk, _physical_local_root
from lftpweb.core.postprocess import perform_remote_delete
from lftpweb.core.preflight import PreflightHold, PreflightRow

logger = logging.getLogger(__name__)

# --- Settings (JSON in `setting`, same pattern as `core/backup.py.BackupSettings`) ---------

SETTING_KEY = "arr_settings"

# Upper bound for `ArrSettings.poll_interval_s`, enforced server-side by `api/settings_arr.py`'s
# PUT handler (2026-08-21, issue #16, `prompts/done/2026-08-21-arr-poll-cadence.md`). There is no
# legitimate reason to want this *arr integration polled slower than once an hour -- this exists
# to catch a fat-fingered value (an extra zero, minutes typed where seconds are expected), not to
# express a real use case. `ArrSyncScheduler.MIN_POLL_INTERVAL_S` is the matching floor.
MAX_POLL_INTERVAL_S = 3600.0


@dataclass(frozen=True)
class ArrSettings:
    """Site-level poll cadence (docs/arr-integration-spec.md "The poller"). Not itself an on/off
    switch -- an instance's own `enabled` column is that (migration 018, "everything defaults
    OFF"); this only governs how often an *enabled* instance's queue is polled.

    **Default 10s, down from 60s** (2026-08-21, issue #16, `prompts/done/2026-08-21-arr-poll-
    cadence.md`: "Preflight progress updating in one-minute jumps, and import detection lagging
    30-60s"). The premise issue #16 argued from -- that a faster cadence multiplies request
    volume against Sonarr/Radarr -- does not hold for this poller's actual shape: the queue poll
    (`ArrClient.queue_records()`) costs exactly **one** HTTP request per bound instance per pass
    for any queue that fits in one `PAGE_SIZE` (250) page, the normal case, so 6x the cadence is
    six requests a minute per instance, not one per item. History
    (`ArrClient.import_events()`) is unaffected by this change at all: it is an exact lookup by
    `downloadId`, called only for items already past the queue-presence check
    (`_check_import`'s requirement 1), so its volume tracks release *transitions*, never poll
    frequency. Both symptoms issue #16 named are gated on this same queue poll -- Preflight's
    progress fields and the two-consecutive-passes import-confirmation guard both read it --
    so lowering it fixes both without a cadence split, an adaptive scheme, or a
    local-observation trick (see docs/decisions.md, 2026-08-21, for the full reasoning behind
    rejecting the split issue #16 itself proposed). Still clamped to
    `ArrSyncScheduler.MIN_POLL_INTERVAL_S` (5s) against a misconfigured near-zero value -- 10s is
    the new *default*, not a new floor. Exposed at Settings -> Integrations as of this change
    (`GET`/`PUT /api/settings/arr/poll-interval`, server-side validated against
    `MIN_POLL_INTERVAL_S`/`MAX_POLL_INTERVAL_S` above); before this it was DB-only, a default
    that got written down rather than ever an actual user choice.
    """

    poll_interval_s: float = 10.0


async def load_arr_settings(db: aiosqlite.Connection) -> ArrSettings:
    cursor = await db.execute("SELECT value FROM setting WHERE key = ?", (SETTING_KEY,))
    row = await cursor.fetchone()
    if row is None:
        return ArrSettings()
    try:
        data = json.loads(row["value"])
    except (ValueError, TypeError):
        return ArrSettings()
    return ArrSettings(poll_interval_s=float(data.get("poll_interval_s", 10.0)))


async def save_arr_settings(db: aiosqlite.Connection, settings: ArrSettings) -> None:
    await db.execute(
        "INSERT INTO setting (key, value, updated_at) VALUES "
        "(?, ?, STRFTIME('%Y-%m-%dT%H:%M:%fZ','now')) "
        "ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (SETTING_KEY, json.dumps({"poll_interval_s": settings.poll_interval_s})),
    )
    await db.commit()


# --- Matching (docs/arr-integration-spec.md "Matching") -------------------------------------

_NORMALIZE_RE = re.compile(r"[._ ]+")


def _normalize_name(name: str) -> str:
    """Case-fold, `.`/`_`/space equivalence (spec: "title normalized (case-fold, `.`/`_`/space
    equivalence)") -- `"Show.S01E05.1080p-GRP"` and `"Show S01E05 1080p-GRP"` normalize to the
    same string.
    """
    return _NORMALIZE_RE.sub(" ", name.casefold()).strip()


def _record_matches_item(record: QueueRecord, item_name: str) -> bool:
    """Match, in the spec's own order: basename of `outputPath` first (exact, the normal case),
    then normalized `title` (covers single-file releases and renaming clients). `item_name` is
    the item's **logical** top-level name (`item.rel_path` for a top-level row is already that
    -- `core/local_scan.py` maps `.downloading-<name>` back to `<name>` before it ever reaches
    the `item` table, so no physical-path handling belongs here; see the five-defects lesson the
    spec itself cites).
    """
    if record.output_path:
        basename = posixpath.basename(record.output_path.rstrip("/"))
        if basename and basename == item_name:
            return True
    if not record.title:
        return False
    return _normalize_name(record.title) == _normalize_name(item_name)


def _derive_arr_root(output_path: str, item_name: str) -> str:
    """The *arr-side root a matched queue record's `outputPath` sits under -- `outputPath`
    itself minus the item's own trailing name segment (`_maybe_warn_path_mismatch`'s own
    docstring, 2026-08-17). Tolerates a trailing filename that doesn't literally equal
    `item_name` -- a single-file release matched via the normalized-title fallback
    (`_record_matches_item` above) can report any filename at all -- by falling back to a plain
    `dirname`; either way the intent is "the parent directory of whatever `outputPath` points
    at," the same root a notify's own translated push (`core/arrnotify.py.
    translate_to_arr_namespace`) must land under for the two to agree.
    """
    normalized = output_path.rstrip("/")
    suffix = "/" + item_name
    if normalized.endswith(suffix):
        return normalized[: -len(suffix)]
    return posixpath.dirname(normalized)


# --- Preflight (docs/transfers-redesign-spec.md §4, prefigured -- this task's own handoff
# prompt, prompts/done/2026-08-20-preflight-box.md) -- releases a bound *arr instance already
# knows about that have not yet reached this seedbox's completed folder, so lftpweb has no
# `item` and no work to do on them yet. **A pure projection of this poller's own latest pass, no
# table, no migration, no persistence** -- every field a row needs is already sitting in the
# `QueueRecord.raw` this module fetches every poll pass (~10s by default) and otherwise discards
# the moment a record
# matches nothing (`_match_items`'s own "candidates" loop never looks at these at all).
#
# **This section is where every *arr-specific piece of the Preflight box lives, deliberately.**
# `core/preflight.py` owns the box's *shared* shape (`PreflightRow`, the flap-tolerance
# `PreflightHold`) precisely so a second source -- non-*arr items held by the settle gate,
# `core/settle.py`, already planned as an immediate follow-up -- can be added there without
# reshaping anything here: matching against `item` names, `arr_visible_path` prefix attribution,
# and the *arr's own `trackedDownloadState`/`downloadId` vocabulary all stay behind this module's
# own boundary, never leaking into the shared row/cache types. ---------------------------------


def _record_identity(record: QueueRecord) -> str:
    """A stable key for one queue record across polls, for `_update_preflight`'s own
    `PreflightHold` below. `download_id` when the client provides one (the normal case -- every
    *arr-tracked download carries its client's own key, docs/transfers-redesign-spec.md §4.4);
    falls back to the normalized title (`_normalize_name`, already used by `_record_matches_item`
    above) for the rare record that doesn't, so a poll-to-poll identity still exists rather than
    the row re-appearing as "new" every single pass.
    """
    if record.download_id:
        return f"id:{record.download_id}"
    return f"title:{_normalize_name(record.title)}"


def _visible_path_contains(arr_visible_path: str, output_path: str) -> bool:
    """Whether `output_path` (the *arr's own reported directory for a queue record) sits under
    `arr_visible_path` (a bound queue's `local_path`, translated into that same *arr's own
    namespace -- `path_queue.arr_visible_path`, already configured on the user's production
    queues per the v0.2.2 diagnosis). A component-boundary check, not a bare `str.startswith`,
    so `/data/tv` does not spuriously swallow `/data/tvshows/...` -- both sides' trailing
    slashes are normalized away first so a configured value with or without one behaves
    identically.
    """
    root = arr_visible_path.rstrip("/")
    if not root:
        return False
    candidate = output_path.rstrip("/")
    return candidate == root or candidate.startswith(root + "/")


def _record_matches_any_item(record: QueueRecord, item_names: frozenset[str]) -> bool:
    """The one implementation of "does this queue record match a real lftpweb item," shared by
    `_preflight_candidates` below (the per-pass exclusion/retirement check) and
    `ArrSyncScheduler.preflight_rows`'s own request-time re-check (2026-08-21, "a handed-over
    release lingers in Preflight for up to 20-30s" -- the poll-cadence term left over after the
    2026-08-21 evict-on-handover fix). Both call sites answer the identical question against the
    identical `_record_matches_item`; a second, independent definition here would let the two
    drift, and drift in either direction is a real user-visible defect -- a row wrongly
    reappearing (drift one way) or a row wrongly vanishing while its download is still genuinely
    in progress (drift the other way).
    """
    return any(_record_matches_item(record, name) for name in item_names)


def _preflight_candidates(
    records: list[QueueRecord],
    queues: list[aiosqlite.Row],
    item_names: frozenset[str],
) -> tuple[list[tuple[int, QueueRecord]], list[QueueRecord]]:
    """Every queue record worth projecting into the Preflight box this pass, paired with the
    queue id it was attributed to -- plus, separately, every record just **retired** by matching
    a real lftpweb item (2026-08-21, "a handed-over release lingers in Preflight for up to
    150s"). Returns `(candidates, retired)`. The handoff prompt's own two rules for `candidates`,
    applied in order:

    1. **"Records that match an lftpweb item are ignored"** -- `item_names` is every top-level
       `item.rel_path` across *every* queue this instance is bound to (gathered fresh by
       `_update_preflight` below, after this pass's own `_match_items` has already run for all of
       them), checked with the exact same `_record_matches_item` the real matcher uses. A record
       this pass just matched into a real item is already excluded here -- the handoff prompt's
       own "no duplicate at handover" requirement, satisfied by construction rather than a
       separate check. **This is also the one branch that means "retired,"** not merely absent:
       the record now corresponds to a real `item` row, a known and terminal reason for it to
       stop being a Preflight row, so it goes in `retired` too -- `_update_preflight` passes that
       straight to `PreflightHold.update`'s own `retired` set so this row is evicted immediately
       rather than held for `PREFLIGHT_HOLD_S` alongside a genuinely-missing one.
    2. **Attribution is `arr_visible_path` prefix-matching, and silence is correct when it
       doesn't resolve.** A record with an `outputPath` is attributed to whichever bound queue's
       `arr_visible_path` contains it (most-specific/longest match wins, for the unlikely case of
       two nested visible paths); a record with **no** `outputPath` at all (the *arr does not
       always populate it) is attributed to the instance's one bound queue only when there is
       exactly one -- never a guess between two or more (the handoff prompt's own instruction).
       No match at all -- omitted, not a fallback guess -- because the sharp risk here is
       promising a file that never arrives.

    **Already-`imported`-at-the-*arr-level records are excluded too**
    (`tracked_download_state == TRACKED_DOWNLOAD_STATE_IMPORTED`) -- the box's own stated scope
    is "still downloading" (step 1 of the handoff prompt's "What to do"), and a record this far
    along is also the one most likely to become a real lftpweb item on literally the next scan;
    dropping it here is one more guard against the same "visible twice" failure mode, on top of
    (not instead of) the item-match check above. **Deliberately not added to `retired`** -- this
    record has not necessarily become any lftpweb item yet (attribution could still fail, or no
    item has been scanned into existence for it at all), so there is no known-terminal fact to
    signal yet; a record excluded for this reason that never becomes a real item simply falls out
    through the ordinary hold-then-expire path once it stops appearing altogether, unchanged.
    """
    out: list[tuple[int, QueueRecord]] = []
    retired: list[QueueRecord] = []
    for record in records:
        if record.tracked_download_state == TRACKED_DOWNLOAD_STATE_IMPORTED:
            continue
        if _record_matches_any_item(record, item_names):
            retired.append(record)
            continue

        queue_id: int | None = None
        if record.output_path:
            best_len = -1
            for queue in queues:
                visible = queue["arr_visible_path"]
                if not visible or not _visible_path_contains(visible, record.output_path):
                    continue
                visible_len = len(visible.rstrip("/"))
                if visible_len > best_len:
                    best_len = visible_len
                    queue_id = queue["id"]
        elif len(queues) == 1:
            queue_id = queues[0]["id"]

        if queue_id is not None:
            out.append((queue_id, record))
    return out, retired


# .NET `TimeSpan.ToString()`'s default format -- what a v3 queue record's own `timeleft` field is
# ("`[d.]hh:mm:ss[.fffffff]`") -- **not verified against a live Sonarr/Radarr instance** (unlike
# `TRACKED_DOWNLOAD_STATE_IMPORTING`/`_IMPORTED` above, this codebase's own module docstring only
# records that verification for `trackedDownloadState`). Tolerates the documented default shape
# and returns `None` for anything else rather than guessing at an undocumented one.
_TIMELEFT_RE = re.compile(
    r"^(?:(?P<days>\d+)\.)?(?P<hours>\d{1,2}):(?P<minutes>\d{2}):(?P<seconds>\d{2})(?:\.\d+)?$"
)


def _parse_timeleft(value: object) -> float | None:
    """`QueueRecord.raw["timeleft"]` -> seconds remaining, for `PreflightRow.remaining_s`
    (2026-08-21, "we missed the remaining time"). Reads straight from `raw` -- no extra request,
    per the handoff prompt's own instruction; `estimatedCompletionTime` (an absolute timestamp)
    was the other field available on the same record but is **not** used here: it would require
    trusting the *arr's clock against this process's own, and a fresh recomputation every request
    (`now` vs. a timestamp read once per poll, ~10s by default) would make the figure visibly
    count down
    unevenly between polls, whereas `timeleft` is already the duration the *arr itself computed
    and is rendered through this codebase's *own* `formatEta`/`transferLineValue` shape exactly
    once it's a plain number of seconds -- one clock (this process's own render), not two.

    `None` for anything not shaped like the documented default .NET `TimeSpan.ToString()` format
    (unparseable), for a missing/non-string value (absent), and for a parsed `00:00:00` (a
    paused/stalled download client item reports this -- meaningless, never a real "0s left," per
    the handoff prompt's own "never a fabricated or zero estimate" instruction).
    """
    if not isinstance(value, str) or not value:
        return None
    match = _TIMELEFT_RE.match(value.strip())
    if match is None:
        return None
    days = int(match.group("days") or 0)
    hours = int(match.group("hours"))
    minutes = int(match.group("minutes"))
    seconds = int(match.group("seconds"))
    total = days * 86400 + hours * 3600 + minutes * 60 + seconds
    return float(total) if total > 0 else None


# Association states a fresh queue record is allowed to match against: never-associated, or a
# terminal one a regrab can restart (spec "Failure modes": "a second record matching an
# already-`cleaned` item name must start a *fresh* association, not resurrect the old one").
# `imported`/`detected`/`notified` are deliberately excluded -- an actively-tracked association
# is never re-matched out from under itself. A match against one of these states is refused when
# the record's own `downloadId` is *identical* to the one already recorded (`_match_items` below)
# -- the same release still sitting in the queue's listing is not a regrab. This refusal is
# deliberately narrower than it looks: it is what keeps a settled `gone`/`cleaned` row from
# spuriously flipping back just because the *arr's queue happens to still (or again) list the
# same download -- see `_REAPPEARANCE_REMATCHABLE_STATES` below for the state that inverts this
# rule on purpose (2026-08-18, `docs/decisions.md`).
_REMATCHABLE_STATES = frozenset({"gone", "cleaned"})

# `dropped` (2026-08-18, this module's own docstring -- the amber grace state, production
# incident) joins the rematch candidates too, but *without* `_REMATCHABLE_STATES`'s
# different-`downloadId` restriction: the identical `downloadId` reappearing in the *arr's queue
# is exactly the direct evidence that the disappearance was a transient blip, not a real removal
# -- the whole point of holding `dropped` rather than committing `gone` outright. `gone`/`cleaned`
# keep the old, stricter rule (a settled row is settled); `dropped` is deliberately not settled
# yet, so it gets the opposite treatment. See `docs/decisions.md`, 2026-08-18, for the full
# reasoning on why these two now diverge.
_REAPPEARANCE_REMATCHABLE_STATES = frozenset({"dropped"})

# States considered "still being watched for import" (spec "The poller" step 3).
_TRACKED_STATES = frozenset({"detected", "notified"})

# `item.state` values a notify-retry attempt is allowed to fire against (spec "Notify":
# fires "after the whole pipeline succeeds"). An item can be matched (`arr_status == 'detected'`)
# well before its own download even finishes -- the *arr's queue is populated by its own
# download client on the seedbox, independently of lftpweb's transfer -- so the retry must not
# push a scan command for a release that is still `REMOTE_ONLY`/`PARTIAL`/`DOWNLOADING`/mid
# transient-postprocess-state; only these three terminal, successful outcomes count.
_NOTIFY_READY_STATES = frozenset({"DOWNLOADED", "VERIFIED", "EXTRACTED"})

# Bounded retry cap (spec "Notify": "bounded retries") -- an instance that is simply down for a
# long stretch gets this many attempts, roughly this many poll intervals apart, before this
# module stops trying; CDH may still import the release on its own regardless.
MAX_NOTIFY_RETRY_ATTEMPTS = 5

# Scan-command outcome verification (2026-08-17) -- how many poll passes `_check_scan_commands`
# will keep asking `GET /api/v3/command/{id}` about a command that never resolves to `completed`
# or `failed` before giving up silently (clearing `item.arr_scan_command_id`, no event). Bounds
# the per-pass API call this check costs to a handful of passes per pushed command, never
# forever -- a command that genuinely never resolves (the *arr restarted mid-run and lost its
# own command history, say) is exactly the same "no evidence either way" case a 404 already is.
MAX_SCAN_COMMAND_CHECK_ATTEMPTS = 5


def _now_iso() -> str:
    """Same wall-clock format `core/audit.py.record_event` stamps `event.ts` with -- one
    convention for "a Python-side UTC timestamp," not a second one invented here.
    """
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _parse_iso(ts: str) -> datetime:
    """The inverse of `_now_iso` -- every `arr_status_at` value this module ever writes was
    produced by that function, so this is the one place that format is parsed back, rather than
    every caller re-deriving it.
    """
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)


# How long a `dropped` row (this module's own docstring, 2026-08-18) is held in the amber
# "rechecking" state before `_check_dropped_items` gives up and commits `gone` -- a deliberate,
# named constant (`docs/concepts.md`), not a settings knob: the production blip that motivated
# this was minutes long, so 6h is generous headroom against a much longer download-client outage
# without holding a genuinely-removed release in limbo for a user-uncomfortable length of time. A
# per-instance/per-queue override is a named future option, not built now.
DROPPED_GONE_GRACE_S = 6 * 3600.0


def _dropped_grace_expired(arr_status_at: str, *, now: datetime | None = None) -> bool:
    """Whether a `dropped` row's grace window (`DROPPED_GONE_GRACE_S`) has elapsed since
    `arr_status_at` (the moment `_commit_dropped` below set it). Wall-clock, not
    `time.monotonic()` -- unlike this module's other bounded-retry bookkeeping (`_InstanceBackoff`
    and friends, all in-memory and restart-loses-it by design), `arr_status_at` is a persisted
    column and must be compared against a persisted, restart-surviving clock; `now` is only ever
    overridden by a test.
    """
    now = now or datetime.now(UTC)
    return (now - _parse_iso(arr_status_at)).total_seconds() >= DROPPED_GONE_GRACE_S


# --- Two-pass quiescence guard (spec "The association lifecycle": "Both signals must hold on
# two consecutive poller passes") -------------------------------------------------------------


@dataclass(frozen=True)
class _PendingVerdict:
    """One item's not-yet-confirmed candidacy for `imported` or `gone`, from the *previous*
    poll pass. `download_id` is carried alongside the verdict so a candidacy computed against
    one association never confirms a *different* one that happens to reach the same item id
    between passes (the regrab case, spec "Failure modes": "Keyed on (item id, downloadId) when
    deciding 'new match'" -- the same discipline applied here to the *confirmation* step, not
    just the match step).
    """

    verdict: Literal["imported", "gone"]
    download_id: str | None


# --- Per-instance failure isolation (spec "The poller": "capped exponential backoff") -------

INITIAL_BACKOFF_S = 60.0
MAX_BACKOFF_S = 1800.0  # 30 minutes
BACKOFF_FACTOR = 2.0


@dataclass
class _InstanceBackoff:
    delay_s: float
    next_attempt_at: float  # time.monotonic()


# --- Rung-4 stranded-source-delete retry sweep (2026-08-17, this module's own docstring above,
# resolving the transient-SSH-failure gap) -----------------------------------------------------

# Bounded, same reasoning as `MAX_NOTIFY_RETRY_ATTEMPTS` above: an item whose delete keeps
# failing gets this many attempts, growing further apart each time (`INITIAL_BACKOFF_S`/
# `BACKOFF_FACTOR`/`MAX_BACKOFF_S`, reused from `_InstanceBackoff` above rather than
# reinvented), before this process pauses and writes one clear event rather than a
# `remote_delete_failed` every poll pass (~10s by default) for as long as a seedbox stays down.
# `remote_delete_pending` is never cleared by pausing -- the manual Files-page delete or a
# restart's clean in-memory slate (`_SourceDeleteRetryState`'s own docstring) can still act.
MAX_SOURCE_DELETE_RETRY_ATTEMPTS = 5


# --- Retroactive self-heal for a row already stranded `gone` before `dropped` existed
# (2026-08-18, this module's own docstring) -------------------------------------------------

# Bounded, same reasoning as `MAX_SOURCE_DELETE_RETRY_ATTEMPTS` above: a genuinely-gone row must
# not accumulate a per-pass `import_events` call forever just because it still owes a source
# delete. Growing-delay backoff (`_GoneHealRetryState` below) spreads the attempts out further
# apart each time, same shape as `_SourceDeleteRetryState`.
MAX_GONE_HEAL_ATTEMPTS = 10


@dataclass
class _GoneHealRetryState:
    """One stranded `gone` row's retroactive-heal bookkeeping -- in-memory, same "restart loses
    it, and that's the safe direction" reasoning as `_SourceDeleteRetryState` above: a restart
    just gets a clean slate and starts counting from attempt 1 again, which the sweep's own query
    (`arr_status='gone' AND remote_delete_pending IS NOT NULL AND remote_deleted_at IS NULL`)
    already guarantees finds the row again regardless.
    """

    attempts: int
    next_attempt_at: float  # time.monotonic()
    paused: bool = False


@dataclass
class _SourceDeleteRetryState:
    """One item's rung-4 retry bookkeeping, present only once this process has tried and failed
    to clear its `remote_delete_pending` debt at least once. In-memory only, the same
    "restart loses it, and that's the safe direction" reasoning as `_pending`/`_notify_attempts`
    above -- a restart gets a clean slate and starts again from attempt 1, which is exactly the
    self-heal `_sweep_stranded_source_deletes` already guarantees on its very first pass, so
    losing this dict on restart costs nothing.
    """

    attempts: int
    next_attempt_at: float  # time.monotonic()
    paused: bool = False


# --- The poller itself ------------------------------------------------------------------------


class ArrSyncScheduler:
    """Background loop, same `_task`/`start()`/`stop()` shape as `core/backup.py.
    BackupScheduler` and `core/local_delete.py`'s (via `core/retention.py`) `RetentionScheduler`
    -- one bad cycle must not kill the loop, `stop()` cancels cleanly on shutdown.

    `config_dir` is needed to decrypt each instance's `api_key_enc` (`core/crypto.py`, same
    convention as the seedbox password) fresh on every poll pass -- an `ArrClient` is
    constructed per instance per pass and closed again immediately after, so a plaintext key
    never outlives the pass that used it. `events` is the same plain-attribute-after-
    construction seam `RetentionScheduler.events` uses; `None` (this module's own tests that
    don't care about the WS side) simply means no `item_delta` is published.

    Phase B (docs/arr-integration-spec.md "Cleanup") adds `in_flight_provider`/
    `delete_in_flight`, the identical seam `core/local_delete.py.RetentionScheduler` takes:
    cleanup's own filesystem removal must be shielded from (and must itself shield) a scan
    racing it, the same "in-memory, protected only while a worker actually holds it" guarantee
    every other deleter in this codebase gets. Both default to `None` (no-op) so every existing
    test of this module that never touches cleanup is unaffected.

    Rung 4 of the move-mode delete ladder (this module's own docstring, 2026-08-16) adds
    `remote_pool`/`host_provider` -- the identical seam `core/postprocess.py.PostprocessPipeline`
    takes for the same job (`RemoteConnectionPool.delete_path`, and the callable that decrypts
    the seedbox host config), loosely typed (`Any`) for the same reason that constructor leaves
    `host_provider` loose. Both default to `None` so every existing test of this module is
    unaffected; production wiring (`main.py`) passes `app.state.engine.pool` and the same
    `_host_provider` closure `PostprocessPipeline` gets.
    """

    MIN_POLL_INTERVAL_S = 5.0  # floor against a misconfigured near-zero setting

    def __init__(
        self,
        db: aiosqlite.Connection,
        config_dir: str,
        events: EventBus | None = None,
        in_flight_provider: Callable[[], frozenset[int]] | None = None,
        delete_in_flight: DeleteInFlight | None = None,
        remote_pool: Any = None,
        host_provider: Any = None,
    ) -> None:
        self.db = db
        self.config_dir = config_dir
        self.events = events
        self.in_flight_provider = in_flight_provider
        self.delete_in_flight = delete_in_flight
        self.remote_pool = remote_pool
        self.host_provider = host_provider
        self._task: asyncio.Task | None = None
        self._backoff: dict[int, _InstanceBackoff] = {}
        self._pending: dict[int, _PendingVerdict] = {}
        # Bounded notify-retry attempts, keyed by item id -- in-memory, same "restart loses
        # pending state, and that is the safe direction" reasoning as `_pending` above (spec:
        # "Notify failure is non-fatal ... bounded retries on subsequent poller ticks").
        self._notify_attempts: dict[int, int] = {}
        # Rung-4 stranded-source-delete retry backoff, keyed by item id -- see
        # `_SourceDeleteRetryState`'s own docstring for why in-memory is the right call here too.
        self._source_delete_retries: dict[int, _SourceDeleteRetryState] = {}
        # Retroactive `gone`-row heal backoff (2026-08-18, `_GoneHealRetryState`'s own docstring),
        # keyed by item id -- same in-memory reasoning as every other bounded-retry dict above.
        self._gone_heal_retries: dict[int, _GoneHealRetryState] = {}
        # Namespace-mismatch warning debounce (2026-08-17, `_maybe_warn_path_mismatch`'s own
        # docstring) -- once per (queue id, derived *arr-side root) per process lifetime, the
        # same in-memory "restart loses it, and that's the safe direction" reasoning as every
        # other per-process dict above (a restart just re-warns once more, never silently).
        self._path_mismatch_warned: set[tuple[int, str]] = set()
        # Multi-page queue observation (2026-08-21, issue #16, `prompts/done/2026-08-21-arr-
        # poll-cadence.md) -- the "one request per pass" property this task's own default-drop
        # leans on (docs/decisions.md) holds only while an instance's queue fits in one
        # `PAGE_SIZE` page; a queue that ever needs a second page is genuinely more expensive to
        # poll at a faster cadence, and that is worth surfacing rather than silently absorbing.
        # In-memory, edge-triggered per instance id -- warns once when a pass *first* observes
        # more than `PAGE_SIZE` records, stays quiet on every subsequent multi-page pass (the
        # same "don't write an event every pass for a continuing condition" idiom
        # `_sweep_stranded_source_deletes` already established), and clears the moment a later
        # pass drops back to one page, so a recurrence is reported again rather than treated as
        # already-known forever. No adaptive backoff is built from this -- purely observational,
        # per the handoff prompt's own instruction not to build a cadence nobody has needed yet.
        self._multi_page_warned: set[int] = set()
        # Scan-command outcome check attempts, keyed by item id (2026-08-17,
        # `_check_scan_commands`'s own docstring) -- bounds the check to
        # `MAX_SCAN_COMMAND_CHECK_ATTEMPTS` passes, in memory: unlike `item.arr_scan_command_id`
        # itself (a persisted column, deliberately -- migration 021's own comment), losing this
        # counter on restart only means a slow-to-resolve command gets a few more free checks
        # than the bound technically allows, never fewer -- the same safe direction
        # `_notify_attempts` above already relies on.
        self._scan_command_checks: dict[int, int] = {}
        # Preflight (this task) -- one `PreflightHold` (`core/preflight.py`) per bound *arr
        # instance id, the flap-tolerant cache of that instance's own last-seen preflight rows.
        # In-memory only, the same "restart loses it, and that's the safe direction" reasoning as
        # every other dict above: a restart just empties the box until the next poll (≤10s by
        # default, 2026-08-21's issue #16; ≤60s before that),
        # which the handoff prompt's own reasoning accepts explicitly rather than adding
        # persistence to avoid it. `_update_preflight` is the only writer; `preflight_rows` is
        # the only reader.
        self._preflight_holds: dict[int, PreflightHold] = {}
        # This instance's own last poll pass's records, keyed by `_record_identity`, and the
        # bound+enabled queue ids that pass attributed against -- both refreshed by
        # `_update_preflight` alongside the hold above, and read by `preflight_rows`' own
        # request-time retirement re-check (2026-08-21, "eviction latency": retirement was only
        # *decided* once per *arr poll, `ArrSettings.poll_interval_s` -- 10s by default as of
        # 2026-08-21's issue #16 (60s before that), even though the underlying question -- "does
        # a matching `item` now exist" -- is purely
        # local and answerable on every request). In-memory only, same "restart loses it, and
        # that's the safe direction" reasoning as every other dict here: a restart just means
        # the very next request's retirement re-check has nothing to test against until the
        # first poll lands, falling back to "not retired" (see `preflight_rows`), which is
        # exactly today's pre-fix behaviour, never worse.
        self._last_records: dict[int, dict[str, QueueRecord]] = {}
        self._last_queue_ids: dict[int, list[int]] = {}

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="lftpweb-arr-sync-loop")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    @property
    def is_alive(self) -> bool:
        return self._task is not None and not self._task.done()

    async def _loop(self) -> None:
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - one bad cycle must not kill the loop
                logger.exception("*arr sync cycle failed")
            settings = await load_arr_settings(self.db)
            await asyncio.sleep(max(settings.poll_interval_s, self.MIN_POLL_INTERVAL_S))

    # --- One pass over every enabled instance ------------------------------------------------

    async def run_once(self) -> None:
        # `notify_on_complete` alongside the pre-existing columns (2026-08-17) -- the
        # namespace-mismatch check (`_maybe_warn_path_mismatch`) skips entirely when it's off,
        # the same "nothing will ever be pushed" reasoning `notify_arr`'s own
        # `"not_configured"` case already uses.
        cursor = await self.db.execute(
            "SELECT id, name, kind, base_url, api_key_enc, notify_on_complete "
            "FROM arr_instance WHERE enabled = 1"
        )
        instances = await cursor.fetchall()
        for instance in instances:
            await self._process_instance(instance)

    async def _process_instance(self, instance: aiosqlite.Row) -> None:
        instance_id = instance["id"]

        backoff = self._backoff.get(instance_id)
        if backoff is not None and time.monotonic() < backoff.next_attempt_at:
            return  # still backing off; never blocks other instances (spec)

        # `SELECT *` -- phase B's notify retry and cleanup need `local_path`/`staging_path`/
        # `arr_visible_path`/`name` alongside `id`/`arr_delete_completed`, and a queue row is
        # cheap; simpler than growing this column list every time a later phase needs one more.
        cursor = await self.db.execute(
            "SELECT * FROM path_queue WHERE arr_instance_id = ? AND enabled = 1",
            (instance_id,),
        )
        queues = await cursor.fetchall()
        if not queues:
            return  # spec: "For each enabled instance with >=1 bound queue"

        try:
            api_key = decrypt_secret(self.config_dir, instance["api_key_enc"])
        except DecryptionError as exc:
            await self._handle_failure(instance_id, instance["name"], exc)
            return

        async with ArrClient(
            kind=instance["kind"], base_url=instance["base_url"], api_key=api_key
        ) as client:
            try:
                records = await client.queue_records()
            except ArrClientError as exc:
                await self._handle_failure(instance_id, instance["name"], exc)
                return

            self._backoff.pop(instance_id, None)  # reachable again
            await self._observe_queue_page_count(instance_id, instance["name"], len(records))

            for queue in queues:
                try:
                    await self._process_queue(
                        client,
                        queue,
                        records,
                        notify_on_complete=bool(instance["notify_on_complete"]),
                    )
                except ArrClientError as exc:
                    # A history lookup mid-pass failed -- the instance just went unreachable
                    # partway through; whatever already committed for earlier queues this pass
                    # stands (each write is its own transaction), the rest waits for the next
                    # attempt after backoff.
                    await self._handle_failure(instance_id, instance["name"], exc)
                    return

            # Preflight (this task) -- after every one of this instance's bound queues has been
            # processed above, so a record `_match_items` just matched into a real item *this
            # very pass* is already excluded from `item_names` below (freshly queried, not the
            # `items` snapshot any one `_process_queue` call took) -- the handoff prompt's own
            # "no duplicate at handover" requirement, holding by construction rather than a
            # separate check. Never reached if the loop above returned early on a mid-pass
            # failure -- this instance's cache simply keeps its last-known contents until a
            # future pass succeeds end to end, the same "unreachable ⇒ keep last known status"
            # direction docs/transfers-redesign-spec.md §4.2 states for phase 2's download
            # clients, applied here too.
            await self._update_preflight(instance, queues, records)

    async def _item_names_for_queue_ids(self, queue_ids: list[int]) -> frozenset[str]:
        """Every top-level `item.rel_path` across `queue_ids` -- the one query behind "does a
        matching lftpweb item exist," shared by `_update_preflight` below (fed its already-
        fetched `queues`) and `preflight_rows`' own request-time re-check (fed
        `_last_queue_ids`, the same set as of this instance's last poll pass -- queue *binding*
        changes are already covered by `preflight_rows`' own caller-supplied
        `enabled_instance_ids`, so this method only ever needs to be freshest about `item`
        existence, the thing this whole fix is about).
        """
        if not queue_ids:
            return frozenset()
        placeholders = ",".join("?" for _ in queue_ids)
        cursor = await self.db.execute(
            f"SELECT rel_path FROM item WHERE queue_id IN ({placeholders}) "  # noqa: S608 - placeholders only, no user input
            "AND instr(rel_path, '/') = 0",
            queue_ids,
        )
        return frozenset(r["rel_path"] for r in await cursor.fetchall())

    async def _update_preflight(
        self, instance: aiosqlite.Row, queues: list[aiosqlite.Row], records: list[QueueRecord]
    ) -> None:
        """Refresh this instance's own `PreflightHold` (`core/preflight.py`) from this pass's
        already-fetched `records` -- see `_preflight_candidates`'s own docstring for the
        matching/attribution/retirement rules and `PreflightHold.update`'s for the flap-tolerance
        hold (and the retirement fast path) applied here.
        """
        queue_ids = [q["id"] for q in queues]
        if not queue_ids:
            return
        item_names = await self._item_names_for_queue_ids(queue_ids)

        # `queue_id` -> the full `path_queue` row it names, so a candidate's queue tag
        # (2026-08-21, "the columns moved around") can be filled in without a second query --
        # `queues` is already `SELECT *`, fetched once per instance per pass by the caller.
        queue_by_id = {q["id"]: q for q in queues}

        candidates, retired_records = _preflight_candidates(records, queues, item_names)
        seen: dict[str, PreflightRow] = {}
        for queue_id, record in candidates:
            queue_row = queue_by_id[queue_id]
            seen[_record_identity(record)] = PreflightRow(
                source="arr",
                queue_id=queue_id,
                queue_name=queue_row["name"],
                queue_short_name=queue_row["short_name"],
                title=record.title,
                status_label=record.tracked_download_state,
                source_label=instance["name"],
                source_kind=instance["kind"],
                size_bytes=record.raw.get("size"),
                size_remaining_bytes=record.raw.get("sizeleft"),
                remaining_s=_parse_timeleft(record.raw.get("timeleft")),
                download_client=record.raw.get("downloadClient"),
                # This source's own wait isn't bound by scan count -- `remaining_s` above
                # already says what it can (`core/preflight.py.PreflightRow.wait_scans`'s own
                # docstring), so both stay unset rather than a fabricated pair.
                wait_scans=None,
                wait_since=None,
            )
        retired = {_record_identity(record) for record in retired_records}
        hold = self._preflight_holds.setdefault(instance["id"], PreflightHold())
        hold.update(seen, now=time.monotonic(), retired=retired)

        # For `preflight_rows`' own request-time retirement re-check below -- this pass's raw
        # records (not just the ones that became candidates) keyed the same way the hold itself
        # is, and the queue ids `item_names` above was just computed against, so a later call
        # with no intervening poll can still re-run the identical predicate.
        self._last_records[instance["id"]] = {_record_identity(r): r for r in records}
        self._last_queue_ids[instance["id"]] = queue_ids

    async def preflight_rows(self, enabled_instance_ids: Iterable[int]) -> list[PreflightRow]:
        """The Preflight box's own read (`api/jobs.py`'s `GET /api/queue/preflight`) -- every
        currently-held row from an instance id in `enabled_instance_ids`. That set is the
        caller's own live "is this instance still enabled, with at least one enabled bound
        queue" check -- an instance disabled (or every one of its queues disabled) after being
        cached simply stops being returned immediately, rather than lingering for
        `core/preflight.py.PREFLIGHT_HOLD_S` for no reason.

        **Now `async`, and now also a request-time retirement check** (2026-08-21, "eviction
        latency"): `_update_preflight`'s own `retired` set already evicts a row the instant a
        *poll pass* discovers the hand-over, but the poll only runs every
        `ArrSettings.poll_interval_s` (10s by default as of 2026-08-21's issue #16, 60s before
        that) -- an item that lands between two polls
        still sat here, visibly duplicated against its own new Active/pending row, for up to
        that whole interval. The underlying question, "does a matching `item` exist now," is
        purely local state, so this re-asks it on every call: for each held row whose identity
        this instance's last poll pass also saw a raw `QueueRecord` for, re-run
        `_record_matches_any_item` (the exact same predicate `_preflight_candidates` uses,
        never a second definition) against a freshly-queried `item_names` set. A held row with
        no corresponding last-seen record (the flap-tolerance case -- it went missing from a
        poll and is being held blind) is passed through unfiltered, same as before this fix:
        there is nothing to re-test it against, and "keep showing it" is the flap-tolerance
        cache's whole point.

        This is a **read-side filter only** -- it never mutates `_preflight_holds`. The next
        real poll pass still does the authoritative eviction via `retired`; skipping that here
        would mean a row that never gets asked about again (no further `GET` calls) never
        actually leaves the hold, which is fine (nothing reads it) but would be a needless
        divergence from "one writer" if this method wrote to it too.

        Sorted by title, case-insensitively -- the *arr's own queue carries no cross-release
        priority signal worth surfacing here (unlike the real transfer queue's `queue_position`),
        so alphabetical is the stable, boring default rather than an invented one.
        """
        allowed = set(enabled_instance_ids)
        rows: list[PreflightRow] = []
        for instance_id, hold in self._preflight_holds.items():
            if instance_id not in allowed:
                continue
            last_records = self._last_records.get(instance_id, {})
            item_names: frozenset[str] | None = None
            for identity, row in hold.items():
                record = last_records.get(identity)
                if record is not None:
                    if item_names is None:
                        item_names = await self._item_names_for_queue_ids(
                            self._last_queue_ids.get(instance_id, [])
                        )
                    if _record_matches_any_item(record, item_names):
                        continue  # a real item now exists -- retire from the response
                rows.append(row)
        rows.sort(key=lambda r: r.title.casefold())
        return rows

    async def _handle_failure(self, instance_id: int, instance_name: str, exc: Exception) -> None:
        """One WARNING, one event row, then back off -- never blocks or slows the loop for
        other instances (spec). `exc` may be an `ArrClientError` (unreachable/non-2xx) or a
        `DecryptionError` (the stored API key can no longer be decrypted, e.g. a rotated
        install secret) -- both mean the same thing to the poller: this instance cannot be
        used right now.
        """
        prior = self._backoff.get(instance_id)
        delay = (
            INITIAL_BACKOFF_S
            if prior is None
            else min(prior.delay_s * BACKOFF_FACTOR, MAX_BACKOFF_S)
        )
        self._backoff[instance_id] = _InstanceBackoff(
            delay_s=delay, next_attempt_at=time.monotonic() + delay
        )
        logger.warning(
            "*arr instance %d (%s) unreachable, backing off %.0fs: %s",
            instance_id,
            instance_name,
            delay,
            exc,
        )
        await audit.record_event(
            self.db,
            level="warning",
            kind="arr_unreachable",
            message=(
                f"*arr instance {instance_name!r} (id={instance_id}) unreachable: {exc}; "
                f"backing off {delay:.0f}s"
            ),
        )

    async def _observe_queue_page_count(
        self, instance_id: int, instance_name: str, record_count: int
    ) -> None:
        """Observational only (2026-08-21, issue #16's own "guard the multi-page case") -- the
        "one request per pass" property `ArrSettings.poll_interval_s`'s new 10s default leans on
        (docs/decisions.md) holds only while `ArrClient.queue_records()` walked exactly one
        `PAGE_SIZE` page; `record_count > PAGE_SIZE` is direct proof it walked more than one
        (`queue_records`'s own pagination stops the instant the running count reaches the
        server's reported total, never over-fetching). Never changes what the poller does --
        no adaptive backoff, no cadence change, per the handoff prompt's own instruction not to
        build one nobody has needed yet -- this only writes one INFO event so a queue that has
        grown past a single page is visible in the audit trail rather than discovered by
        surprise.
        """
        if record_count <= PAGE_SIZE:
            self._multi_page_warned.discard(instance_id)  # back to one page -- rearm the warning
            return
        if instance_id in self._multi_page_warned:
            return  # already reported for this instance; don't spam every pass
        self._multi_page_warned.add(instance_id)
        logger.info(
            "*arr instance %d (%s) queue spans multiple pages (%d records, page size %d) -- "
            "polling this instance now costs more than one request per pass",
            instance_id,
            instance_name,
            record_count,
            PAGE_SIZE,
        )
        await audit.record_event(
            self.db,
            level="info",
            kind="arr_queue_multi_page",
            message=(
                f"*arr instance {instance_name!r} (id={instance_id}) queue spans multiple "
                f"pages ({record_count} records, page size {PAGE_SIZE}) -- polling this "
                "instance now costs more than one queue request per pass; no behavior change"
            ),
        )

    # --- One bound queue, given the instance's already-fetched queue records -----------------

    async def _process_queue(
        self,
        client: ArrClient,
        queue: aiosqlite.Row,
        records: list[QueueRecord],
        *,
        notify_on_complete: bool,
    ) -> None:
        queue_id = queue["id"]
        cursor = await self.db.execute(
            "SELECT id, rel_path, arr_status, arr_download_id, state, pending_download_prefix, "
            "remote_delete_pending FROM item WHERE queue_id = ? AND instr(rel_path, '/') = 0",
            (queue_id,),
        )
        items = await cursor.fetchall()

        await self._match_items(queue, items, records, notify_on_complete=notify_on_complete)

        tracked = [i for i in items if i["arr_status"] in _TRACKED_STATES]
        for item in tracked:
            if item["arr_status"] == "detected":
                # Phase B (spec "Notify"): the *primary* push already happened, or didn't
                # happen at all yet, from `PostprocessPipeline`'s own tail -- this is only the
                # bounded retry for a primary attempt that failed (`notify_arr`'s own
                # `item.arr_status == 'detected'` gate is what actually decides "notified"
                # never re-enters this branch, not this `if`).
                await self._maybe_retry_notify(queue, item)
            await self._check_import(client, queue, item, records)

        # `dropped` rows (2026-08-18, this module's own docstring) -- rechecked every pass, not
        # gated behind another two-pass observation, since `dropped` itself already *is* the
        # "held for confirmation" state. A fresh query, not the stale `items` snapshot above:
        # `_match_items` may have just rematched some of these rows back to `detected` this very
        # pass (the same-`downloadId`-reappeared case), and those must not also be evaluated here
        # against pre-match data.
        await self._check_dropped_items(client, queue)

        # Retroactive heal for a row that already committed `gone` before `dropped` existed
        # (2026-08-18) -- ahead of the rung-4 sweep below, same "resolve the *arr status first,
        # then chase the delete debt" ordering `_check_import`/`_check_dropped_items` already
        # follow, so a row this promotes to `imported` this very pass has its rung-4 delete
        # already attempted (inside `_commit_terminal`) before the sweep below even queries.
        await self._heal_stranded_gone_rows(client, queue)

        # Rung-4 retry sweep (this module's own docstring, 2026-08-17) -- after the
        # import-check loop above (so a delete that finally clears this very pass is already
        # gone before the `arr_delete_completed` cleanup sweep below queries), keyed off the
        # debt itself rather than the `imported` transition, so a transient SSH failure gets
        # tried again next pass instead of stranding the remote copy permanently.
        await self._sweep_stranded_source_deletes(queue)

        # Scan-command outcome verification (2026-08-17) -- independent of the ladder/cleanup
        # work above, so its ordering relative to them doesn't matter; every item this queue is
        # still tracking a pushed command's outcome for (`item.arr_scan_command_id` non-null,
        # queried fresh inside), regardless of `arr_status`.
        await self._check_scan_commands(client, queue)

        if queue["arr_delete_completed"]:
            # A fresh query, not the stale `items` snapshot above -- `_check_import` may have
            # just committed an item to `imported` this very pass (spec: "Withheld is
            # re-evaluated on later passes, not terminal" implies the reverse too: a
            # newly-imported item must not wait an extra pass before cleanup is even
            # considered). It also runs *after* `_check_import`'s own rung-4 remote delete
            # (`_commit_terminal` -> `_maybe_delete_remote_on_import`), so an imported item this
            # very pass already has its remote copy gone before this sweep even queries --
            # "import green -> delete source -> (optionally) delete local," per the ladder.
            cursor = await self.db.execute(
                "SELECT * FROM item WHERE queue_id = ? AND arr_status = 'imported'", (queue_id,)
            )
            imported_items = await cursor.fetchall()
            for item in imported_items:
                await self._maybe_cleanup(queue, item)

    # --- Notify retry (spec "Notify": "retry on the next poller tick (bounded retries)") -----

    async def _maybe_retry_notify(self, queue: aiosqlite.Row, item: aiosqlite.Row) -> None:
        """Retry a notify push whose primary attempt (`PostprocessPipeline`'s own tail) already
        failed -- or push for the first time, if the primary attempt never got the chance to
        (e.g. this instance's `notify_on_complete` was turned on after the item's pipeline run
        already finished). Gated on the item having reached a stable, successful local outcome
        (`_NOTIFY_READY_STATES`, no pending download-prefix rename, no active job) -- the *arr's
        own queue can (and normally does) list a release before lftpweb has even finished
        pulling it down, so `arr_status == 'detected'` alone is not evidence the pipeline is
        done.
        """
        item_id = item["id"]
        if item["state"] not in _NOTIFY_READY_STATES or item["pending_download_prefix"] is not None:
            return
        if self._notify_attempts.get(item_id, 0) >= MAX_NOTIFY_RETRY_ATTEMPTS:
            return

        cursor = await self.db.execute(
            "SELECT 1 FROM job WHERE item_id = ? AND state IN ('queued', 'running') LIMIT 1",
            (item_id,),
        )
        if await cursor.fetchone() is not None:
            return  # not stable yet -- don't push mid-job

        final_root = await self._resolve_final_physical_root(queue, item["rel_path"])
        if final_root is None:
            return  # bytes not found at either known location this pass -- try again later

        outcome = await notify_arr(
            self.db,
            config_dir=self.config_dir,
            item=item,
            queue=queue,
            final_local_root=final_root,
            events=self.events,
        )
        if outcome == "failed":
            self._notify_attempts[item_id] = self._notify_attempts.get(item_id, 0) + 1
        else:
            self._notify_attempts.pop(item_id, None)

    # --- Shared path resolution: notify retry and cleanup both need "where are this item's
    # bytes right now" ---------------------------------------------------------------------

    async def _resolve_final_physical_root(
        self, queue: aiosqlite.Row, rel_path: str
    ) -> Path | None:
        """Where this item's bytes actually are, for a push or a delete that must act on the
        real location. Always asks `core/local_delete.py._physical_local_root` first -- the one
        resolver for the download-prefix-in-flight case, never a second one (per this project's
        own five-defects lesson). That resolver only ever accounts for the download-prefix
        namespace, never a queue's own `auto_move` relocation to `staging_path`
        (`core/postprocess.py._do_move`) -- so when its answer doesn't exist on disk, the one
        other place a finished item's bytes can legitimately be is `staging_path/rel_path`,
        checked here as a narrow, named fallback specific to this concern, not a general-purpose
        second resolver for the concern `_physical_local_root` already owns.

        `None` when neither location has anything on disk -- a legitimate outcome (bytes not
        there yet, or already gone), not an error; every caller treats it as "nothing to do this
        pass."
        """
        root = Path(queue["local_path"].rstrip("/"))
        candidate = await _physical_local_root(
            self.db, queue_id=queue["id"], root=root, rel_path=rel_path
        )
        if candidate.exists() or candidate.is_symlink():
            return candidate
        staging = queue["staging_path"]
        if staging:
            candidate2 = Path(staging.rstrip("/")) / rel_path
            if candidate2.exists() or candidate2.is_symlink():
                return candidate2
        return None

    # --- Cleanup (spec "Cleanup") -------------------------------------------------------------

    async def _maybe_cleanup(self, queue: aiosqlite.Row, item: aiosqlite.Row) -> None:
        """For an item whose association reached `imported` on an `arr_delete_completed` queue:
        withhold (named reason, re-evaluated next pass -- never terminal) when verification for
        this item failed or a job is active for it; otherwise suppress re-download, then remove
        the local bytes, then record `cleaned`.

        **Deliberately never writes `item.state`.** See this module's own docstring for why:
        the bytes disappearing is left for the ordinary scan + `core/mount_sentinel.py`
        absence-grace machinery to discover and carry to `REMOVED_LOCAL` on its own clock,
        exactly as if an external mover (a human, or the *arr's own hardlink pickup) had taken
        them -- "no new timer," per the spec.
        """
        item_id = item["id"]
        queue_id = queue["id"]

        if item["remote_delete_pending"] is not None:
            # Restores "delete source -> delete local" as an *enforced* ladder order rather
            # than a hoped-for one (2026-08-17, this module's own docstring) -- before this
            # check, cleanup ran regardless of the debt and could remove the local copy while
            # the remote copy was still stranded, exactly the production incident this task
            # fixes. `_sweep_stranded_source_deletes` retries the source delete every pass
            # (including this one, ahead of this cleanup sweep in `_process_queue`'s own
            # ordering), so the very next pass after it finally clears is the very next pass
            # cleanup is reconsidered here -- no extra timer, no state massaging. A `copy`
            # queue never sets `remote_delete_pending` (rung 4 is a `move`-only concept), so
            # this branch is a no-op there, matching the pre-existing behavior exactly.
            await self._record_cleanup_withheld(
                item_id,
                queue,
                "a deferred source delete is still pending (remote_delete_pending) -- "
                "ladder order requires delete source before delete local",
            )
            return

        if item["state"] == "CORRUPT":
            await self._record_cleanup_withheld(
                item_id, queue, "verification for this item failed (state=CORRUPT)"
            )
            return

        cursor = await self.db.execute(
            "SELECT 1 FROM job WHERE item_id = ? AND state IN ('queued', 'running') LIMIT 1",
            (item_id,),
        )
        if await cursor.fetchone() is not None:
            await self._record_cleanup_withheld(
                item_id, queue, "an active job exists for this item"
            )
            return

        # Suppress FIRST, before anything on disk is touched (spec "Cleanup" step 1) --
        # belt-and-braces against a copy-mode queue's auto-queue re-grabbing the still-present
        # remote copy while cleanup is in flight.
        await self.db.execute("UPDATE item SET auto_queue_suppressed = 1 WHERE id = ?", (item_id,))
        await self.db.commit()

        local_root = await self._resolve_final_physical_root(queue, item["rel_path"])
        if local_root is not None:
            local_path_root = Path(queue["local_path"].rstrip("/"))
            resolved = extract.resolve_within_root(local_root, local_path_root)
            if resolved is None and queue["staging_path"]:
                # The candidate may legitimately have come from `staging_path` instead (an
                # `auto_move` queue) -- re-check containment against *that* root before giving
                # up, rather than declaring an escape against a root the candidate was never
                # claiming to be under.
                resolved = extract.resolve_within_root(
                    local_root, Path(queue["staging_path"].rstrip("/"))
                )
            if resolved is None:
                await self._record_cleanup_withheld(
                    item_id,
                    queue,
                    f"{local_root} resolves outside the queue's known roots -- refusing",
                )
                return
            if not mount_sentinel.check(queue["local_path"].rstrip("/")):
                await self._record_cleanup_withheld(
                    item_id,
                    queue,
                    "local root is missing, unreadable, or has not completed a mount-sentinel scan",
                )
                return

            in_flight = self.in_flight_provider() if self.in_flight_provider else frozenset()
            if item_id in in_flight:
                await self._record_cleanup_withheld(
                    item_id, queue, "a post-processing worker is currently running for this item"
                )
                return
            if (
                self.delete_in_flight is not None
                and item_id in self.delete_in_flight.in_flight_item_ids()
            ):
                await self._record_cleanup_withheld(
                    item_id, queue, "a delete is already in progress for this item"
                )
                return

            if self.delete_in_flight is not None:
                self.delete_in_flight.mark([item_id])
            try:
                await asyncio.to_thread(_do_remove_from_disk, local_root, resolved)
            except OSError as exc:
                await self._record_cleanup_withheld(item_id, queue, f"local delete failed: {exc}")
                return
            finally:
                if self.delete_in_flight is not None:
                    self.delete_in_flight.unmark([item_id])
        # else: nothing found at either known location -- the goal state (no local copy) already
        # holds, so this proceeds to record `cleaned` rather than withholding forever on an item
        # that's already gone.

        await self.db.execute(
            "UPDATE item SET arr_status = 'cleaned', arr_status_at = ? WHERE id = ?",
            (_now_iso(), item_id),
        )
        await self.db.commit()
        await audit.record_event(
            self.db,
            level="info",
            item_id=item_id,
            kind="arr_cleanup",
            message=(
                f"queue {queue_id} ({queue['name']!r}): local copy removed after confirmed *arr "
                "import -- item.state left as-is for the normal absence-grace machinery to carry "
                "it to REMOVED_LOCAL, same as any externally-caused removal"
            ),
        )
        await self._publish_item(queue_id, item_id)

    async def _record_cleanup_withheld(
        self, item_id: int, queue: aiosqlite.Row, reason: str
    ) -> None:
        await audit.record_event(
            self.db,
            level="warning",
            item_id=item_id,
            kind="arr_cleanup_withheld",
            message=f"queue {queue['id']} ({queue['name']!r}): cleanup withheld -- {reason}",
        )

    # --- Matching: (no status) | gone | cleaned -> detected ----------------------------------

    async def _match_items(
        self,
        queue: aiosqlite.Row,
        items: list[aiosqlite.Row],
        records: list[QueueRecord],
        *,
        notify_on_complete: bool,
    ) -> None:
        candidates = [
            i
            for i in items
            if i["arr_status"] is None
            or i["arr_status"] in _REMATCHABLE_STATES
            or i["arr_status"] in _REAPPEARANCE_REMATCHABLE_STATES
        ]
        if not candidates:
            return

        used_record_ids: set[int] = set()
        for item in candidates:
            matched: QueueRecord | None = None
            for record in records:
                if id(record) in used_record_ids:
                    continue
                if not _record_matches_item(record, item["rel_path"]):
                    continue
                # A terminal association only restarts on a genuinely *different* downloadId
                # (spec: "a second record matching an already-`cleaned` item name must start a
                # fresh association, not resurrect the old one" -- the identical downloadId
                # reappearing is not a regrab, it's the same release still sitting in the
                # queue's listing, and must not spuriously flip a settled `gone`/`cleaned` row
                # back to `detected`). Deliberately does NOT apply to `_REAPPEARANCE_REMATCHABLE_
                # STATES` (`dropped`, 2026-08-18) -- there, the identical downloadId reappearing
                # is exactly the evidence that confirms the disappearance was a blip; see
                # `_REAPPEARANCE_REMATCHABLE_STATES`'s own docstring.
                if (
                    item["arr_status"] in _REMATCHABLE_STATES
                    and record.download_id is not None
                    and record.download_id == item["arr_download_id"]
                ):
                    continue
                matched = record
                break
            if matched is not None:
                used_record_ids.add(id(matched))
                await self._commit_match(
                    queue, item, matched, notify_on_complete=notify_on_complete
                )

    async def _commit_match(
        self,
        queue: aiosqlite.Row,
        item: aiosqlite.Row,
        record: QueueRecord,
        *,
        notify_on_complete: bool,
    ) -> None:
        queue_id = queue["id"]
        is_regrab = item["arr_status"] in _REMATCHABLE_STATES
        is_reappearance = item["arr_status"] in _REAPPEARANCE_REMATCHABLE_STATES
        await self.db.execute(
            "UPDATE item SET arr_status = 'detected', arr_status_at = ?, arr_download_id = ? "
            "WHERE id = ?",
            (_now_iso(), record.download_id, item["id"]),
        )
        await self.db.commit()
        message = (
            f"matched *arr queue record (downloadId={record.download_id!r}, "
            f"outputPath={record.output_path!r})"
        )
        if is_regrab:
            message += f" -- fresh association, prior state was {item['arr_status']!r} (regrab)"
        elif is_reappearance:
            message += (
                " -- reappeared with the same downloadId after briefly dropping out of the "
                "*arr's queue; confirms the disappearance was a transient blip, not a removal"
            )
        await audit.record_event(
            self.db, level="info", kind="arr_matched", item_id=item["id"], message=message
        )
        await self._publish_item(queue_id, item["id"])
        await self._maybe_warn_path_mismatch(
            queue, item, record, notify_on_complete=notify_on_complete
        )

    # --- Namespace-mismatch detection (2026-08-17, production evidence:
    # private_data/debug_logs/productionlftpweb.log) --------------------------------------------

    async def _maybe_warn_path_mismatch(
        self,
        queue: aiosqlite.Row,
        item: aiosqlite.Row,
        record: QueueRecord,
        *,
        notify_on_complete: bool,
    ) -> None:
        """The user's *arr instances mount the same storage at a different path than lftpweb
        does (`/mnt/seanas02_media/Working/box-dc-tv` vs lftpweb's own
        `/mnt/seanas02-media-working/box-dc-tv`). With `arr_visible_path` unset, every notify
        pushed lftpweb's own path -- the *arr accepted the scan command (201) and silently
        scanned a directory that doesn't exist in *its* container, so imports waited on the
        *arr's own unrelated schedule instead of the push, and several associations drifted all
        the way to `gone`. The evidence to catch this was already in hand: the matched queue
        record's own `outputPath` is the *arr's view of this exact release, so a namespace
        mismatch is detectable the moment a match commits -- well before the first notify ever
        fires. `core/arrsync.py`'s own `arr_scan_command_failed` (a later addition, this same
        task) is the *confirmed* counterpart to this *predictive* one: this fires from a path
        comparison alone, before any push has even happened.

        **Detection only -- changes no behavior.** The notify still fires exactly as it does
        today (this is advisory, not a gate); a false positive -- an exotic remote-path-mapping
        setup where a mismatch is actually intentional -- costs one event, worded to allow for
        that.

        Skipped entirely, no event, in the cases where there is nothing to say: `record.
        output_path` is `None` (a title-fallback match has no *arr-side path to compare against
        at all), or `notify_on_complete` is off for this instance (nothing will ever be pushed,
        so a mismatch here is moot -- matches this module's "everything defaults off produces
        zero events, not noise" convention, same as `notify_arr`'s own `"not_configured"` case).

        Debounced once per `(queue id, derived *arr-side root)` per process lifetime
        (`self._path_mismatch_warned`) -- every subsequent match against the same misconfigured
        queue would otherwise repeat the identical advisory every poll pass for as long as the
        setting stays wrong.
        """
        if record.output_path is None or not notify_on_complete:
            return

        item_name = item["rel_path"]
        push_full = translate_to_arr_namespace(
            f"{queue['local_path'].rstrip('/')}/{item_name}",
            local_path=queue["local_path"],
            staging_path=queue["staging_path"],
            arr_visible_path=queue["arr_visible_path"],
        )
        push_root = posixpath.dirname(push_full.rstrip("/"))
        arr_root = _derive_arr_root(record.output_path, item_name)
        if push_root == arr_root:
            return

        debounce_key = (queue["id"], arr_root)
        if debounce_key in self._path_mismatch_warned:
            return
        self._path_mismatch_warned.add(debounce_key)

        await audit.record_event(
            self.db,
            level="warning",
            item_id=item["id"],
            kind="arr_path_mismatch",
            message=(
                f"queue {queue['id']} ({queue['name']!r}): a notify for this item would push "
                f"{push_root!r} but the *arr reports its own path for this release as "
                f"{record.output_path!r} (root {arr_root!r}) -- these look like different "
                "filesystem namespaces, so the push likely lands nowhere real on the *arr's "
                "side. If this is intentional (an unusual remote-path-mapping setup), ignore "
                f"this. Otherwise, set this queue's 'Path as seen by the *arr' to {arr_root!r}."
            ),
        )

    # --- Scan-command outcome verification (2026-08-17, production evidence:
    # private_data/debug_logs/productionlftpweb.log) -- `notify_arr`'s push was otherwise
    # fire-and-forget: a 201 only means "command queued", never "the *arr could act on this
    # path". `arr_scan_command_failed` below is the *confirmed* counterpart to
    # `arr_path_mismatch` above's *predictive* one -- that one fires from a path comparison
    # alone, before any push has happened; this one fires from the *arr's own eventual verdict
    # on a push that already went out. -----------------------------------------------------

    async def _check_scan_commands(self, client: ArrClient, queue: aiosqlite.Row) -> None:
        """Every item this queue is still tracking a pushed scan command's outcome for
        (`item.arr_scan_command_id` non-null, queried fresh -- independent of `arr_status`,
        since import can resolve before or after the command itself does) gets one
        `GET /api/v3/command/{id}` this pass. Persisted, not in-memory, because `notify_arr` is
        called from two different processes' objects (`core/postprocess.py`'s primary push,
        this module's own bounded notify-retry) -- see migration 021's own comment for why a
        restart must not orphan the check the way it safely orphans this module's other
        in-memory bookkeeping.
        """
        cursor = await self.db.execute(
            "SELECT id, rel_path, arr_scan_command_id FROM item "
            "WHERE queue_id = ? AND arr_scan_command_id IS NOT NULL",
            (queue["id"],),
        )
        for row in await cursor.fetchall():
            await self._check_one_scan_command(client, queue, row)

    async def _check_one_scan_command(
        self, client: ArrClient, queue: aiosqlite.Row, row: aiosqlite.Row
    ) -> None:
        item_id = row["id"]
        raw = await client.get_command(row["arr_scan_command_id"])

        if raw is None:
            # 404 -- pruned or unknown (the *arr prunes finished commands after a while, or
            # restarted and lost its own command history). No evidence either way, not a
            # failure: clear silently, same as a resolved outcome below.
            await self._clear_scan_command(item_id)
            self._scan_command_checks.pop(item_id, None)
            return

        outcome = command_outcome(raw)
        if outcome == "pending":
            attempts = self._scan_command_checks.get(item_id, 0) + 1
            if attempts >= MAX_SCAN_COMMAND_CHECK_ATTEMPTS:
                # Bounded, per this module's own docstring above -- never let a command that
                # genuinely never resolves accumulate a per-pass API call forever. Silent, like
                # the 404 case: this is "give up checking," not "the push failed."
                await self._clear_scan_command(item_id)
                self._scan_command_checks.pop(item_id, None)
                return
            self._scan_command_checks[item_id] = attempts
            return

        self._scan_command_checks.pop(item_id, None)
        await self._clear_scan_command(item_id)
        if outcome == "completed":
            # The push at least executed; import detection remains the authority on whether
            # anything actually got imported from it.
            return

        # "failed" -- the confirmed counterpart to `_maybe_warn_path_mismatch`'s predictive one.
        push_full = translate_to_arr_namespace(
            f"{queue['local_path'].rstrip('/')}/{row['rel_path']}",
            local_path=queue["local_path"],
            staging_path=queue["staging_path"],
            arr_visible_path=queue["arr_visible_path"],
        )
        await audit.record_event(
            self.db,
            level="warning",
            item_id=item_id,
            kind="arr_scan_command_failed",
            message=(
                f"queue {queue['id']} ({queue['name']!r}): the *arr scan command pushed for "
                f"{push_full!r} did not complete successfully -- if the *arr cannot see this "
                "path, set this queue's 'Path as seen by the *arr' to the path the *arr "
                "actually mounts this content under"
            ),
        )

    async def _clear_scan_command(self, item_id: int) -> None:
        await self.db.execute("UPDATE item SET arr_scan_command_id = NULL WHERE id = ?", (item_id,))
        await self.db.commit()

    # --- Import/removal detection: detected|notified -> imported | gone ----------------------

    async def _check_import(
        self,
        client: ArrClient,
        queue: aiosqlite.Row,
        item: aiosqlite.Row,
        records: list[QueueRecord],
    ) -> None:
        item_id = item["id"]
        download_id: str | None = item["arr_download_id"]

        if download_id is not None:
            current = next((r for r in records if r.download_id == download_id), None)
        else:
            # Defensive fallback for an association matched with no downloadId available (a
            # single-file release the title-fallback matched) -- name-based, same as matching
            # itself, per the spec's own "history lookup by name is fuzzy" acknowledgment.
            current = next((r for r in records if _record_matches_item(r, item["rel_path"])), None)

        # Requirement 1 (spec): "The queue record is gone (or reports
        # trackedDownloadState: imported)". Present with any other state -- including
        # `importing` -- means "not yet", full stop; the pending guard resets rather than
        # merely pausing, since a fresh two consecutive passes must observe both signals once
        # the record does leave.
        if (
            current is not None
            and current.tracked_download_state != TRACKED_DOWNLOAD_STATE_IMPORTED
        ):
            self._pending.pop(item_id, None)
            return

        # Requirement 2: >=1 history import event for the release.
        history: list[HistoryEvent] = await client.import_events(
            download_id=download_id,
            source_title=None if download_id else item["rel_path"],
        )
        has_import_event = any(e.is_import_event() for e in history)
        candidate_verdict: Literal["imported", "gone"] = "imported" if has_import_event else "gone"

        # Requirement 3: both signals held on two consecutive passes.
        prior = self._pending.get(item_id)
        if (
            prior is not None
            and prior.verdict == candidate_verdict
            and prior.download_id == download_id
        ):
            self._pending.pop(item_id, None)
            if candidate_verdict == "imported":
                await self._commit_terminal(queue, item, "imported", len(history))
            else:
                # 2026-08-18 (this module's own docstring, production incident): the two-pass
                # guard confirming "no import evidence" no longer commits terminal `gone`
                # directly -- it commits the amber `dropped` grace state instead.
                # `_check_dropped_items` takes it from there on every subsequent pass.
                await self._commit_dropped(queue, item)
        else:
            self._pending[item_id] = _PendingVerdict(
                verdict=candidate_verdict, download_id=download_id
            )

    async def _commit_dropped(self, queue: aiosqlite.Row, item: aiosqlite.Row) -> None:
        """`detected`/`notified` -> `dropped` (2026-08-18, this module's own docstring): the
        two-pass quiescence guard confirmed the *arr's queue record is gone with no import
        history event, but rather than treating that as settled (the old, direct-to-`gone`
        behavior), this holds the row in an amber "rechecking" state for `DROPPED_GONE_GRACE_S`
        -- `_check_dropped_items` re-evaluates it every subsequent pass rather than gating behind
        another two-pass observation, since `dropped` itself already *is* the held-for-
        confirmation state.
        """
        await self.db.execute(
            "UPDATE item SET arr_status = 'dropped', arr_status_at = ? WHERE id = ?",
            (_now_iso(), item["id"]),
        )
        await self.db.commit()
        await audit.record_event(
            self.db,
            level="info",
            kind="arr_queue_dropped",
            item_id=item["id"],
            message=(
                "*arr queue record disappeared with no import history event -- holding amber "
                f"for {DROPPED_GONE_GRACE_S / 3600:.0f}h before calling it gone; rechecking "
                "every pass (2026-08-17/18 production incident: a download-client blank-queue "
                "blip flipped 8 items straight to gone in a single pass while lftpweb was still "
                "downloading them -- support bundle "
                "lftpweb-support-0.2.3-20260818T013532Z, docs/decisions.md)"
            ),
        )
        await self._publish_item(queue["id"], item["id"])

    async def _commit_terminal(
        self,
        queue: aiosqlite.Row,
        item: aiosqlite.Row,
        verdict: Literal["imported", "gone"],
        import_event_count: int,
        *,
        source: Literal["quiescence", "dropped", "gone_heal"] = "quiescence",
    ) -> None:
        """`source` only changes the audit-event wording, never the transition itself --
        `"quiescence"` is the original two-pass-guard confirmation (`_check_import`, `verdict`
        always `"imported"` since 2026-08-18: the `"gone"` candidate now routes through
        `_commit_dropped` instead, see that method's own docstring); `"dropped"` is
        `_check_dropped_items` (either an import surfaced during the grace window, or the window
        itself expired with neither signal); `"gone_heal"` is `_heal_stranded_gone_rows`
        promoting a row that had already committed terminal `gone` before this shipped.
        """
        queue_id = queue["id"]
        await self.db.execute(
            "UPDATE item SET arr_status = ?, arr_status_at = ? WHERE id = ?",
            (verdict, _now_iso(), item["id"]),
        )
        await self.db.commit()
        if verdict == "imported":
            if source == "dropped":
                kind, message = (
                    "arr_imported",
                    f"*arr history shows {import_event_count} import event(s) for this release, "
                    "confirmed after the queue record had earlier dropped out of the *arr's "
                    "queue with no import evidence (arr_status was 'dropped') -- the "
                    "disappearance resolved within the grace window",
                )
            elif source == "gone_heal":
                kind, message = (
                    "arr_imported",
                    f"*arr history now shows {import_event_count} import event(s) for this "
                    "release -- promoted from a stranded 'gone' verdict by the retroactive heal "
                    "sweep (2026-08-18); a deferred source delete/cleanup, if still owed, "
                    "proceeds normally from here",
                )
            else:
                kind, message = (
                    "arr_imported",
                    f"*arr queue record gone/imported with {import_event_count} import history "
                    "event(s), confirmed on two consecutive poller passes",
                )
        else:
            if source == "dropped":
                kind, message = (
                    "arr_gone",
                    f"unconfirmed for {DROPPED_GONE_GRACE_S / 3600:.0f}h after leaving the "
                    "*arr's queue (arr_status was 'dropped') -- no import history event "
                    "appeared within the grace window; local files untouched, no cleanup "
                    "performed",
                )
            else:
                kind, message = (
                    "arr_gone",
                    "*arr queue record disappeared with no import history event, confirmed on "
                    "two consecutive poller passes -- local files untouched, no cleanup "
                    "performed",
                )
            if item["remote_delete_pending"] is not None:
                # Visibility only, no behavior change (2026-08-17, this module's own docstring)
                # -- rung 4 never fires on `gone` (by design: ambiguity must not trigger an
                # irreversible delete), so a deferred source delete that was still owed when the
                # *arr's queue record vanished just sits stranded silently otherwise. Production
                # evidence: 15 items went `notified` -> `gone` with `remote_delete_pending`
                # still set, and each source sat on the seedbox with nothing in History
                # explaining why.
                message += (
                    " -- a deferred source delete was still pending for this item; it remains "
                    "withheld (rung 4 never fires on `gone`, by design) -- manual deletion from "
                    "the Files page is the intended path"
                )
        await audit.record_event(
            self.db, level="info", kind=kind, item_id=item["id"], message=message
        )
        await self._publish_item(queue_id, item["id"])

        if verdict == "imported":
            # Rung 4 of the move-mode delete ladder (this module's own docstring) -- runs
            # before this pass's `arr_delete_completed` cleanup sweep (`_process_queue`'s own
            # ordering: `_check_import` -> here -> the cleanup loop), so "import green -> delete
            # source -> (optionally) delete local" holds even within a single poller pass.
            # Never called on `gone`.
            await self._maybe_delete_remote_on_import(queue, item["id"])

    # --- `dropped`: rechecked every pass (this module's own docstring, 2026-08-18) -----------

    async def _check_dropped_items(self, client: ArrClient, queue: aiosqlite.Row) -> None:
        """Every row this queue is currently holding at `arr_status = 'dropped'`, re-evaluated
        this pass. A fresh query, not a stale snapshot -- a row `_match_items` (run earlier this
        same pass, in `_process_queue`) just rematched back to `detected` (the same-`downloadId`-
        reappeared case) is already gone from this query's own result, so it is not
        double-processed against pre-match data.

        Deliberately not gated by another two-pass observation the way `_check_import`'s
        `detected`/`notified` -> `dropped` transition is -- `dropped` itself already *is* the
        held-for-confirmation state; gating it again would just double the grace window for no
        added safety.
        """
        cursor = await self.db.execute(
            "SELECT id, rel_path, arr_download_id, arr_status_at, remote_delete_pending "
            "FROM item WHERE queue_id = ? AND arr_status = 'dropped'",
            (queue["id"],),
        )
        for row in await cursor.fetchall():
            await self._check_one_dropped_item(client, queue, row)

    async def _check_one_dropped_item(
        self, client: ArrClient, queue: aiosqlite.Row, row: aiosqlite.Row
    ) -> None:
        """One `dropped` row's turn: an import history event promotes it straight to `imported`
        (no further quiescence wait -- the row already spent a pass or more absent from the
        queue, and an import event is strong, not ambiguous, evidence); otherwise, once
        `arr_status_at` is older than `DROPPED_GONE_GRACE_S`, it finally commits `gone`. Neither
        condition holding yet is a silent no-op -- the row just stays `dropped` for another pass.
        """
        download_id = row["arr_download_id"]
        history: list[HistoryEvent] = await client.import_events(
            download_id=download_id,
            source_title=None if download_id else row["rel_path"],
        )
        if any(e.is_import_event() for e in history):
            await self._commit_terminal(queue, row, "imported", len(history), source="dropped")
            return
        if _dropped_grace_expired(row["arr_status_at"]):
            await self._commit_terminal(queue, row, "gone", len(history), source="dropped")

    # --- Rung 4 of the move-mode delete ladder (this module's own docstring, 2026-08-16) -----

    async def _maybe_delete_remote_on_import(self, queue: aiosqlite.Row, item_id: int) -> bool:
        """`core/postprocess.py._maybe_delete_remote` defers a `move`-mode item's delete here
        (`item.remote_delete_pending` non-null) the moment it discovers, at the tail of its own
        pipeline run, that the item is *arr-tracked -- rungs 1-3 (completeness, verify, extract)
        had already cleared *then*, which is exactly what authorizes performing the delete now,
        unconditionally, once `_commit_terminal` confirms `imported`. No re-derivation of
        verify/extract state happens here: `remote_delete_pending` carries the verify evidence
        forward (`'VERIFIED'` or `'SKIPPED'`) so the eventual delete event reads exactly as
        informative as an immediate rung-3 delete's, via the same `perform_remote_delete`.

        A no-op, deliberately, for: a `copy`/`sync` queue (`remote_delete_pending` is never set
        for those); an item that was never deferred, including one `_maybe_delete_remote` found
        `CORRUPT`/`EXTRACT_FAILED` (that function clears the column on those branches rather
        than setting it, so "CORRUPT vetoes at every rung" holds all the way out here too); an
        item whose remote copy is already gone (`remote_deleted_at` set -- idempotent against a
        queue record briefly reappearing); and a process that never wired `remote_pool`/
        `host_provider` (a test fixture that doesn't exercise this feature -- the item simply
        stays deferred for a later pass, the same as a missing host in the immediate rung-3
        case).

        **Called from two places now** (2026-08-17): `_commit_terminal`'s one-shot call on the
        `imported` transition (unchanged), and `_sweep_stranded_source_deletes`'s per-pass retry
        for a debt that first attempt failed to clear -- both share this one implementation,
        never a second one, same as the rest of this codebase's delete plumbing.

        Returns whether the debt is resolved by the time this call returns: `True` once
        `remote_deleted_at` is set (by this call or an earlier one) or there is genuinely
        nothing for this process to do about it (wrong sync mode, feature not wired, or the row
        already shows the debt cleared/deleted) -- none of those are a "failure" the retry sweep
        should back off on. `False` only for a real, still-outstanding failure (no host
        configured, or `perform_remote_delete` itself failed) that the sweep should retry again
        later.
        """
        if queue["sync_mode"] != "move" or self.remote_pool is None or self.host_provider is None:
            return True  # nothing this process will ever do about it -- not a failure to retry

        cursor = await self.db.execute(
            "SELECT rel_path, remote_delete_pending, remote_deleted_at FROM item WHERE id = ?",
            (item_id,),
        )
        row = await cursor.fetchone()
        if (
            row is None
            or row["remote_delete_pending"] is None
            or row["remote_deleted_at"] is not None
        ):
            return True  # debt already resolved (or the item is gone) -- nothing to retry

        queue_id = queue["id"]
        host = await self.host_provider()
        if host is None:
            await audit.record_event(
                self.db,
                level="error",
                item_id=item_id,
                kind="remote_delete_withheld",
                message=(
                    f"queue {queue_id} ({queue['name']!r}) mode=move: delete withheld -- "
                    "no host configured"
                ),
            )
            return False

        remote_full = queue["remote_path"].rstrip("/") + "/" + row["rel_path"]
        ok = await perform_remote_delete(
            self.db,
            self.remote_pool,
            host,
            item_id=item_id,
            queue_id=queue_id,
            queue_name=queue["name"],
            remote_full=remote_full,
            verify_state=row["remote_delete_pending"],
        )
        if ok:
            await self.db.execute(
                "UPDATE item SET remote_delete_pending = NULL WHERE id = ?", (item_id,)
            )
            await self.db.commit()
        await self._publish_item(queue_id, item_id)
        return ok

    async def _sweep_stranded_source_deletes(self, queue: aiosqlite.Row) -> None:
        """Retry sweep for rung 4's deferred source delete (2026-08-17, this module's own
        docstring). Re-asks `_maybe_delete_remote_on_import` every pass for every item this
        queue is still carrying a `remote_delete_pending` debt for -- keyed off the debt itself,
        not the one-shot `imported` transition that first created it, which is what turns a
        transient SSH failure (the production incident this task fixes) into something that
        gets tried again next pass instead of stranding the remote copy permanently.

        Keyed off the debt rather than the transition also means a row already stranded before
        this shipped -- `imported` or already `cleaned`, remote copy still alive,
        `remote_delete_pending` still set from the original one-shot attempt -- matches this
        same query and is retried on the very first pass after upgrade. No migration, no state
        massaging; the query alone is the self-heal. `arr_status IN ('imported', 'cleaned')`
        names both terminal-import outcomes explicitly (rather than just `'imported'`) for
        exactly that reason: `_maybe_cleanup`'s own new gate below means a *fresh* `cleaned` row
        can no longer carry a pending debt going forward, but a row that reached `cleaned` before
        this fix shipped already did, and still needs to be swept.

        Short-circuits before querying when the feature isn't wired this process
        (`remote_pool`/`host_provider` both `None`, true of most of this module's own tests) --
        the identical no-op `_maybe_delete_remote_on_import` itself falls back to, but skips the
        query and the backoff bookkeeping too, so an unwired fixture never accumulates state in
        `_source_delete_retries` it has no reason to.
        """
        if self.remote_pool is None or self.host_provider is None:
            return
        cursor = await self.db.execute(
            "SELECT id FROM item WHERE queue_id = ? AND remote_delete_pending IS NOT NULL "
            "AND arr_status IN ('imported', 'cleaned') AND remote_deleted_at IS NULL",
            (queue["id"],),
        )
        for row in await cursor.fetchall():
            await self._retry_stranded_source_delete(queue, row["id"])

    async def _retry_stranded_source_delete(self, queue: aiosqlite.Row, item_id: int) -> None:
        """One item's turn in the sweep above -- backoff bookkeeping around the shared
        `_maybe_delete_remote_on_import` call. Same growing-delay shape as `_InstanceBackoff`
        (module-level `INITIAL_BACKOFF_S`/`BACKOFF_FACTOR`/`MAX_BACKOFF_S`, reused rather than
        reinvented) but bounded at `MAX_SOURCE_DELETE_RETRY_ATTEMPTS`: past that, one
        `remote_delete_retries_paused` event fires -- not a `remote_delete_failed` every pass
        for as long as a seedbox stays down -- and this process stops trying.
        `remote_delete_pending` stays set throughout either way, so the manual Files-page delete
        or a restart's clean-slate sweep (`_SourceDeleteRetryState`'s own docstring) can still
        clear it.
        """
        state = self._source_delete_retries.get(item_id)
        if state is not None and (state.paused or time.monotonic() < state.next_attempt_at):
            return

        ok = await self._maybe_delete_remote_on_import(queue, item_id)
        if ok:
            self._source_delete_retries.pop(item_id, None)
            return

        attempts = (state.attempts if state is not None else 0) + 1
        if attempts >= MAX_SOURCE_DELETE_RETRY_ATTEMPTS:
            self._source_delete_retries[item_id] = _SourceDeleteRetryState(
                attempts=attempts, next_attempt_at=time.monotonic(), paused=True
            )
            await audit.record_event(
                self.db,
                level="warning",
                item_id=item_id,
                kind="remote_delete_retries_paused",
                message=(
                    f"queue {queue['id']} ({queue['name']!r}) mode=move: deferred source "
                    f"delete has failed {attempts} times -- pausing automatic retries; "
                    "remote_delete_pending stays set, so the manual Files-page delete or a "
                    "lftpweb restart's fresh sweep can still clear it"
                ),
            )
            return

        delay = min(INITIAL_BACKOFF_S * (BACKOFF_FACTOR ** (attempts - 1)), MAX_BACKOFF_S)
        self._source_delete_retries[item_id] = _SourceDeleteRetryState(
            attempts=attempts, next_attempt_at=time.monotonic() + delay
        )

    # --- Retroactive heal for a row already stranded `gone` before `dropped` existed
    # (2026-08-18, this module's own docstring) -------------------------------------------------

    async def _heal_stranded_gone_rows(self, client: ArrClient, queue: aiosqlite.Row) -> None:
        """A row that already committed terminal `gone` before this fix shipped (the production
        8, and any like them) can still be carrying a stranded rung-4 delete debt --
        `core/postprocess.py._maybe_delete_remote` defers on *any* non-null `arr_status`,
        `gone` included, so a `move`-mode item whose pipeline run only finished *after* the
        `gone` verdict committed still got `remote_delete_pending` set. Keyed off the debt
        itself, the same "the query alone is the self-heal, no migration" shape
        `_sweep_stranded_source_deletes` already established for the analogous
        transient-SSH-failure case: `arr_status = 'gone' AND remote_delete_pending IS NOT NULL
        AND remote_deleted_at IS NULL`. Runs unconditionally (not gated on `remote_pool`/
        `host_provider` being wired) -- promoting a wrongly-stuck `gone` row to `imported` is
        worth doing for the *arr-status/icon alone, independent of whether this process can also
        act on the delete debt; `_commit_terminal`'s own `imported` branch already no-ops safely
        when the delete plumbing isn't wired, the same as every other caller of it.
        """
        cursor = await self.db.execute(
            "SELECT id, rel_path, arr_download_id, remote_delete_pending FROM item "
            "WHERE queue_id = ? AND arr_status = 'gone' AND remote_delete_pending IS NOT NULL "
            "AND remote_deleted_at IS NULL",
            (queue["id"],),
        )
        for row in await cursor.fetchall():
            await self._heal_one_gone_row(client, queue, row)

    async def _heal_one_gone_row(
        self, client: ArrClient, queue: aiosqlite.Row, row: aiosqlite.Row
    ) -> None:
        """One stranded `gone` row's turn -- backoff bookkeeping around the shared
        `import_events` lookup, same growing-delay shape as `_retry_stranded_source_delete`
        above but bounded at `MAX_GONE_HEAL_ATTEMPTS`: past that, one `arr_gone_heal_giving_up`
        event fires and this process stops asking. `remote_delete_pending` (and `arr_status`)
        stay exactly as they were either way -- a restart's clean in-memory slate, or the query
        itself on the very next pass after a restart, picks the row back up regardless.
        """
        item_id = row["id"]
        state = self._gone_heal_retries.get(item_id)
        if state is not None and (state.paused or time.monotonic() < state.next_attempt_at):
            return

        download_id = row["arr_download_id"]
        if download_id is None:
            # No downloadId ever recorded for this association (a title-fallback match, or a row
            # that predates matching recording one at all) -- there is no exact history lookup
            # possible. Counts as an attempt anyway, per this sweep's own hard cap, so a row like
            # this doesn't sit exempt from ever giving up either.
            history: list[HistoryEvent] = []
        else:
            history = await client.import_events(download_id=download_id)

        if any(e.is_import_event() for e in history):
            self._gone_heal_retries.pop(item_id, None)
            await self._commit_terminal(queue, row, "imported", len(history), source="gone_heal")
            return

        attempts = (state.attempts if state is not None else 0) + 1
        if attempts >= MAX_GONE_HEAL_ATTEMPTS:
            self._gone_heal_retries[item_id] = _GoneHealRetryState(
                attempts=attempts, next_attempt_at=time.monotonic(), paused=True
            )
            await audit.record_event(
                self.db,
                level="warning",
                item_id=item_id,
                kind="arr_gone_heal_giving_up",
                message=(
                    f"queue {queue['id']} ({queue['name']!r}): checked *arr history "
                    f"{attempts} times for an import that would promote this stranded 'gone' "
                    "row -- none found; giving up automatic rechecks. A deferred source delete "
                    "(remote_delete_pending) is still parked -- manual deletion from the Files "
                    "page is the intended path"
                ),
            )
            return

        delay = min(INITIAL_BACKOFF_S * (BACKOFF_FACTOR ** (attempts - 1)), MAX_BACKOFF_S)
        self._gone_heal_retries[item_id] = _GoneHealRetryState(
            attempts=attempts, next_attempt_at=time.monotonic() + delay
        )

    # --- Publish (persist -> read back -> publish, DESIGN.md §2.2) ---------------------------

    async def _publish_item(self, queue_id: int, item_id: int) -> None:
        if self.events is None:
            return
        cursor = await self.db.execute("SELECT * FROM item WHERE id = ?", (item_id,))
        row = await cursor.fetchone()
        if row is None:
            return
        if row["state"] == "REMOVED_BOTH":
            # A row that has left both trees is out of the published projection
            # (`core/engine.py._project`'s rel_paths filter) and off the Files page. An
            # `item_delta` for it would resurrect a dead node in every connected client --
            # visible, un-actionable (no local copy to delete, no remote copy to queue), and
            # only cleared by the next connect-time snapshot. Seen live 2026-08-16: files
            # deleted by hand, then the *arr queue record removed, and the `gone` commit's
            # publish put both rows back on the page. The state write and audit event above
            # still happen; only the WS publish is skipped. (`REMOVED_LOCAL` still publishes:
            # its remote copy keeps it in the projection.)
            return
        self.events.publish({"type": "item_delta", "queue_id": queue_id, "nodes": [item_view(row)]})
