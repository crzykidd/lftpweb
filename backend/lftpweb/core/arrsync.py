"""The *arr sync poller (docs/arr-integration-spec.md "The poller") -- background loop, same
`_task`/`start()`/`stop()` shape as `core/backup.py.BackupScheduler`, matching a bound Sonarr/
Radarr instance's `/api/v3/queue` against local items, and watching for import (or removal).

**Not wired into the scan pass** -- scan cadence is per-queue and variable (DESIGN.md §5), and
*arr polling wants its own clock, independent of it (spec: "not wired into the scan pass").

**Scope of this phase** (`prompts/2026-08-15-arr-integration-backend.md`, phase A of 3): matching
(`(no status) -> detected`) and import/removal detection (`detected/notified -> imported/gone`).
**Notify and cleanup are phase B** -- this module never POSTs the scan command and never deletes
anything. That is not a departure from the spec: the spec's own "Build plan" section scopes
phase 1 to exactly "poller with match + import detection", with "Notify + cleanup" named as
phase 2 separately. The lifecycle section's step 4 ("For imported items on a
`arr_delete_completed` queue: run cleanup") describes the *finished* feature once all three
phases are combined, not what phase A alone builds.

**The "notified" state is unreachable in this phase** for the same reason: it is set only when
`PostprocessPipeline`'s tail POSTs the scan command (phase B), which does not exist yet. Every
item this module can produce reads `detected`, `imported`, or `gone`. The import-detection code
below still checks `state IN ('detected', 'notified')` rather than hardcoding `'detected'`
alone, so it needs no change once phase B lands.

**The two-consecutive-passes quiescence guard is in-memory, not persisted** (deliberately: the
spec's "Data model" section specifies exactly three new `item` columns and no new table for this
phase, unlike `core/settle.py`'s `item_settle`). A restart loses any pending candidacy and simply
costs one extra poll interval before a transition commits -- safe, since "wait longer before the
irreversible step" is the direction restart-loss is allowed to err in for a feature this module's
own phase does not even reach (cleanup is phase B). See `_PendingVerdict` below.
"""

from __future__ import annotations

import asyncio
import json
import logging
import posixpath
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

import aiosqlite

from lftpweb.core import audit
from lftpweb.core.arrclient import (
    IMPORT_EVENT_TYPES,
    TRACKED_DOWNLOAD_STATE_IMPORTED,
    ArrClient,
    ArrClientError,
    HistoryEvent,
    QueueRecord,
)
from lftpweb.core.crypto import DecryptionError, decrypt_secret
from lftpweb.core.events import EventBus
from lftpweb.core.itemview import item_view

logger = logging.getLogger(__name__)

# --- Settings (JSON in `setting`, same pattern as `core/backup.py.BackupSettings`) ---------

SETTING_KEY = "arr_settings"


@dataclass(frozen=True)
class ArrSettings:
    """Site-level poll cadence (docs/arr-integration-spec.md "The poller": "Default every 60s
    (site setting, `ArrSettings` in `setting` like every other settings dataclass)"). Not itself
    an on/off switch -- an instance's own `enabled` column is that (migration 018, "everything
    defaults OFF"); this only governs how often an *enabled* instance's queue is polled.
    """

    poll_interval_s: float = 60.0


async def load_arr_settings(db: aiosqlite.Connection) -> ArrSettings:
    cursor = await db.execute("SELECT value FROM setting WHERE key = ?", (SETTING_KEY,))
    row = await cursor.fetchone()
    if row is None:
        return ArrSettings()
    try:
        data = json.loads(row["value"])
    except (ValueError, TypeError):
        return ArrSettings()
    return ArrSettings(poll_interval_s=float(data.get("poll_interval_s", 60.0)))


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


# Association states a fresh queue record is allowed to match against: never-associated, or a
# terminal one a regrab can restart (spec "Failure modes": "a second record matching an
# already-`cleaned` item name must start a *fresh* association, not resurrect the old one").
# `imported`/`detected`/`notified` are deliberately excluded -- an actively-tracked association
# is never re-matched out from under itself.
_REMATCHABLE_STATES = frozenset({"gone", "cleaned"})

# States considered "still being watched for import" (spec "The poller" step 3). `notified` is
# unreachable in this phase (see module docstring) but included so this needs no edit once
# phase B lands.
_TRACKED_STATES = frozenset({"detected", "notified"})


def _now_iso() -> str:
    """Same wall-clock format `core/audit.py.record_event` stamps `event.ts` with -- one
    convention for "a Python-side UTC timestamp," not a second one invented here.
    """
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


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
    """

    MIN_POLL_INTERVAL_S = 5.0  # floor against a misconfigured near-zero setting

    def __init__(
        self,
        db: aiosqlite.Connection,
        config_dir: str,
        events: EventBus | None = None,
    ) -> None:
        self.db = db
        self.config_dir = config_dir
        self.events = events
        self._task: asyncio.Task | None = None
        self._backoff: dict[int, _InstanceBackoff] = {}
        self._pending: dict[int, _PendingVerdict] = {}

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
        cursor = await self.db.execute(
            "SELECT id, name, kind, base_url, api_key_enc FROM arr_instance WHERE enabled = 1"
        )
        instances = await cursor.fetchall()
        for instance in instances:
            await self._process_instance(instance)

    async def _process_instance(self, instance: aiosqlite.Row) -> None:
        instance_id = instance["id"]

        backoff = self._backoff.get(instance_id)
        if backoff is not None and time.monotonic() < backoff.next_attempt_at:
            return  # still backing off; never blocks other instances (spec)

        cursor = await self.db.execute(
            "SELECT id, arr_delete_completed FROM path_queue "
            "WHERE arr_instance_id = ? AND enabled = 1",
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

            for queue in queues:
                try:
                    await self._process_queue(client, queue, records)
                except ArrClientError as exc:
                    # A history lookup mid-pass failed -- the instance just went unreachable
                    # partway through; whatever already committed for earlier queues this pass
                    # stands (each write is its own transaction), the rest waits for the next
                    # attempt after backoff.
                    await self._handle_failure(instance_id, instance["name"], exc)
                    return

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

    # --- One bound queue, given the instance's already-fetched queue records -----------------

    async def _process_queue(
        self, client: ArrClient, queue: aiosqlite.Row, records: list[QueueRecord]
    ) -> None:
        queue_id = queue["id"]
        cursor = await self.db.execute(
            "SELECT id, rel_path, arr_status, arr_download_id FROM item "
            "WHERE queue_id = ? AND instr(rel_path, '/') = 0",
            (queue_id,),
        )
        items = await cursor.fetchall()

        await self._match_items(queue_id, items, records)

        tracked = [i for i in items if i["arr_status"] in _TRACKED_STATES]
        for item in tracked:
            await self._check_import(client, queue_id, item, records)

    # --- Matching: (no status) | gone | cleaned -> detected ----------------------------------

    async def _match_items(
        self, queue_id: int, items: list[aiosqlite.Row], records: list[QueueRecord]
    ) -> None:
        candidates = [
            i for i in items if i["arr_status"] is None or i["arr_status"] in _REMATCHABLE_STATES
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
                # back to `detected`).
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
                await self._commit_match(queue_id, item, matched)

    async def _commit_match(self, queue_id: int, item: aiosqlite.Row, record: QueueRecord) -> None:
        is_regrab = item["arr_status"] in _REMATCHABLE_STATES
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
        await audit.record_event(
            self.db, level="info", kind="arr_matched", item_id=item["id"], message=message
        )
        await self._publish_item(queue_id, item["id"])

    # --- Import/removal detection: detected|notified -> imported | gone ----------------------

    async def _check_import(
        self,
        client: ArrClient,
        queue_id: int,
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
        has_import_event = any(e.event_type in IMPORT_EVENT_TYPES for e in history)
        candidate_verdict: Literal["imported", "gone"] = "imported" if has_import_event else "gone"

        # Requirement 3: both signals held on two consecutive passes.
        prior = self._pending.get(item_id)
        if (
            prior is not None
            and prior.verdict == candidate_verdict
            and prior.download_id == download_id
        ):
            await self._commit_terminal(queue_id, item, candidate_verdict, len(history))
            self._pending.pop(item_id, None)
        else:
            self._pending[item_id] = _PendingVerdict(
                verdict=candidate_verdict, download_id=download_id
            )

    async def _commit_terminal(
        self,
        queue_id: int,
        item: aiosqlite.Row,
        verdict: Literal["imported", "gone"],
        import_event_count: int,
    ) -> None:
        await self.db.execute(
            "UPDATE item SET arr_status = ?, arr_status_at = ? WHERE id = ?",
            (verdict, _now_iso(), item["id"]),
        )
        await self.db.commit()
        if verdict == "imported":
            kind, message = (
                "arr_imported",
                f"*arr queue record gone/imported with {import_event_count} import history "
                "event(s), confirmed on two consecutive poller passes",
            )
        else:
            kind, message = (
                "arr_gone",
                "*arr queue record disappeared with no import history event, confirmed on two "
                "consecutive poller passes -- local files untouched, no cleanup performed",
            )
        await audit.record_event(
            self.db, level="info", kind=kind, item_id=item["id"], message=message
        )
        await self._publish_item(queue_id, item["id"])

    # --- Publish (persist -> read back -> publish, DESIGN.md §2.2) ---------------------------

    async def _publish_item(self, queue_id: int, item_id: int) -> None:
        if self.events is None:
            return
        cursor = await self.db.execute("SELECT * FROM item WHERE id = ?", (item_id,))
        row = await cursor.fetchone()
        if row is not None:
            self.events.publish(
                {"type": "item_delta", "queue_id": queue_id, "nodes": [item_view(row)]}
            )
