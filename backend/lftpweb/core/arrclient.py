"""Sonarr/Radarr v3 API client (docs/arr-integration-spec.md "Scope"/"The poller").

One async class over `httpx`, not two -- the two APIs are shape-identical for everything this
codebase touches (`system/status`, `queue`, `history`, `command`); only the command *name* and
media noun differ, so `kind` is a constructor argument, not a subclass split. `X-Api-Key` header,
10s timeout, matching `docs/arr-integration-spec.md`'s "The poller" section verbatim.

**The `eventType`/`trackedDownloadState` vocabulary below is now verified against a live Sonarr
v3 instance** (2026-08-15, via the lftpweb audit trail on a real run + this fix -- see
`docs/decisions.md`), superseding the community-documentation-only constants this module
shipped with. `trackedDownloadState`'s strings (`"importing"`, `"imported"`) were already
correct. `eventType` was not: the v3 API serializes it in **response bodies as a camelCase
string** (`"downloadFolderImported"`, `"grabbed"`, ...) -- the numeric codes exist only as
*query-parameter* values, never as a response body field's actual type, so a numeric
`IMPORT_EVENT_TYPES` could never match a real history record (two live Sonarr imports were
misclassified `gone` on the first real run before this was caught). `IMPORT_EVENT_TYPES` is
therefore string-keyed; the historical numeric code is kept as a defensive fallback only (see
`HistoryEvent.is_import_event`), for a serializer setting or *arr version this codebase hasn't
seen, at effectively zero cost.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx

logger = logging.getLogger(__name__)

ArrKind = Literal["sonarr", "radarr"]

DEFAULT_TIMEOUT_S = 10.0

# One page walk's page size -- large enough that a normal home install's queue/history fits on
# one page (the common case makes zero extra requests), small enough not to ask either API for
# an unbounded response body.
PAGE_SIZE = 250

# *arr v3 history `eventType` values -- **verified against a live Sonarr v3 instance,
# 2026-08-15, see module docstring.** `downloadFolderImported` is the one this codebase cares
# about: it is the per-file trailing signal a completed import leaves behind
# (docs/arr-integration-spec.md "The association lifecycle" -- "one history import event lands
# *after each file's copy completes*"). `grabbed` and `downloadFailed` are recorded here for
# completeness/future use, not read by `core/arrsync.py` in this phase.
EVENT_TYPE_GRABBED = "grabbed"
EVENT_TYPE_DOWNLOAD_FOLDER_IMPORTED = "downloadFolderImported"
EVENT_TYPE_DOWNLOAD_FAILED = "downloadFailed"

# The only event type(s) that count as "this release was imported" for the lifecycle's
# requirement 2 (docs/arr-integration-spec.md). A `frozenset` (not a single constant) because a
# future correction against a live instance may find more than one event type qualifies (e.g. a
# per-episode vs. per-season variant) without every caller changing shape. String-keyed --
# `HistoryEvent.is_import_event` is where this is actually consulted; nothing compares
# `event_type` to this set directly, so a future addition here needs no call-site changes.
IMPORT_EVENT_TYPES: frozenset[str] = frozenset({EVENT_TYPE_DOWNLOAD_FOLDER_IMPORTED})

# Pre-2026-08-15 assumption, kept only as `HistoryEvent.is_import_event`'s numeric fallback --
# **never** compared against directly elsewhere. The wire format is a string (see module
# docstring); this exists solely so an *arr version or serializer setting that somehow still
# emits the historical numeric code in a response body is tolerated too, cheaply.
_LEGACY_EVENT_TYPE_DOWNLOAD_FOLDER_IMPORTED = 3

# *arr v3 queue record `trackedDownloadState` vocabulary -- **verified against a live Sonarr v3
# instance, 2026-08-15, see module docstring** (this vocabulary was already correct pre-fix).
# `importing` is the value the spec's lifecycle section leans on directly: a record still
# reporting it is *never* `imported`, no matter what history says (requirement 1's "not yet"
# case for a slow multi-file import).
TRACKED_DOWNLOAD_STATE_IMPORTING = "importing"
TRACKED_DOWNLOAD_STATE_IMPORTED = "imported"

# Sonarr/Radarr differ only in the command name pushed to trigger an import scan
# (docs/arr-integration-spec.md "Notify") -- not called by anything in this phase (phase B wires
# it into `PostprocessPipeline.process_item`'s tail); built now so the client is complete.
_SCAN_COMMAND_NAME: dict[ArrKind, str] = {
    "sonarr": "DownloadedEpisodesScan",
    "radarr": "DownloadedMoviesScan",
}


class ArrClientError(Exception):
    """The instance could not be reached, or responded with a non-2xx status.

    Callers (`core/arrsync.py`) treat this uniformly as "this instance is unreachable right
    now" -- the per-instance failure isolation the spec requires -- never distinguishing DNS
    failure from a 500 from a timeout; none of those distinctions change what the poller does
    (log once, write one event row, back off).
    """


@dataclass(frozen=True)
class QueueRecord:
    """One `/api/v3/queue` record, the fields `core/arrsync.py`'s matcher needs -- `raw` keeps
    the full response dict alongside so a future caller (or a test) can read a field this
    projection doesn't name yet without a client change.
    """

    download_id: str | None
    title: str
    output_path: str | None
    tracked_download_state: str | None
    raw: dict[str, Any] = field(repr=False)


@dataclass(frozen=True)
class HistoryEvent:
    """One `/api/v3/history` record -- same "narrow projection + raw" shape as `QueueRecord`.

    `event_type` is typed `str | int | None` rather than narrowed to `str` because the raw
    field is stored exactly as the response body serialized it -- normally a camelCase string
    (`"downloadFolderImported"`), but `is_import_event` below also tolerates the legacy numeric
    form, so the type here has to admit both rather than lying about what `raw.get("eventType")`
    can actually hand back.
    """

    event_type: str | int | None
    download_id: str | None
    source_title: str | None
    raw: dict[str, Any] = field(repr=False)

    def is_import_event(self) -> bool:
        """Whether this event counts as "this release was imported"
        (docs/arr-integration-spec.md "The association lifecycle", requirement 2) -- the one
        place this comparison happens, so callers (`core/arrsync.py`) never compare
        `event_type` against `IMPORT_EVENT_TYPES` (or the legacy numeric code) directly.

        The real wire format is the camelCase string form (verified against a live Sonarr v3
        instance, 2026-08-15 -- see module docstring); a numeric `event_type` -- never seen live,
        kept only for tolerance -- is compared against the pre-fix numeric assumption instead,
        cheaply, so any *arr version or serializer setting that emits it is still handled
        correctly rather than silently misclassified as `gone` the way the numeric-only
        comparison this replaces was.
        """
        if isinstance(self.event_type, str):
            return self.event_type in IMPORT_EVENT_TYPES
        return self.event_type == _LEGACY_EVENT_TYPE_DOWNLOAD_FOLDER_IMPORTED


class ArrClient:
    """One instance per `arr_instance` row, constructed with its decrypted API key -- callers
    never hold a plaintext key longer than the client's own lifetime (`core/arrsync.py`
    constructs one per poll pass per instance via `async with`, and closes it again
    immediately after that instance's pass finishes).
    """

    def __init__(
        self,
        *,
        kind: ArrKind,
        base_url: str,
        api_key: str,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        self.kind = kind
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"X-Api-Key": api_key},
            timeout=timeout_s,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> ArrClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        try:
            response = await self._client.get(path, params=params)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise ArrClientError(f"{self.kind} GET {path} failed: {exc}") from exc
        return response.json()

    async def system_status(self) -> dict[str, Any]:
        """`GET /api/v3/system/status` -- the Test-connection round trip
        (`api/settings_arr.py`'s `/api/settings/arr/{id}/test`): reachability plus the
        instance's own reported version.
        """
        return await self._get("/api/v3/system/status")

    async def queue_records(self) -> list[QueueRecord]:
        """Walk *every page* of `/api/v3/queue` -- a busy instance can exceed one page (spec's
        own "Failure modes" warning) -- and return every record, not just top-level ones or
        matched ones; filtering to bound queues and top-level items is `core/arrsync.py`'s job,
        not this client's.
        """
        records: list[QueueRecord] = []
        page = 1
        while True:
            data = await self._get("/api/v3/queue", params={"page": page, "pageSize": PAGE_SIZE})
            raw_records = data.get("records", [])
            for raw in raw_records:
                records.append(
                    QueueRecord(
                        download_id=raw.get("downloadId"),
                        title=raw.get("title") or "",
                        output_path=raw.get("outputPath"),
                        tracked_download_state=raw.get("trackedDownloadState"),
                        raw=raw,
                    )
                )
            total = data.get("totalRecords", len(raw_records))
            # Stop once an empty page is seen or the running count of records actually
            # received reaches the server's own reported total -- **not** `page * PAGE_SIZE`,
            # which silently under-walks if the server honors a smaller page size than
            # requested (real APIs are free to cap it; this must not assume they don't).
            if not raw_records or len(records) >= total:
                break
            page += 1
        return records

    async def import_events(
        self, *, download_id: str | None = None, source_title: str | None = None
    ) -> list[HistoryEvent]:
        """`GET /api/v3/history`, walked across every page, filtered by `downloadId` when known
        (exact -- spec: "History lookup by name is fuzzy; by `downloadId` it is exact") or by
        `sourceTitle` as the fallback when it isn't. Returns every history event for the
        release, not pre-filtered to import events -- `core/arrsync.py` decides which
        `eventType`s count (`IMPORT_EVENT_TYPES` above), so this stays a plain data fetch.

        A caller passing neither filter gets an empty list without a request -- there is no
        "every history event on the instance" use case here, and skipping the request avoids
        asking a real *arr for its entire history on a matching bug.
        """
        if download_id is None and source_title is None:
            return []
        base_params: dict[str, Any] = {"pageSize": PAGE_SIZE}
        if download_id is not None:
            base_params["downloadId"] = download_id
        else:
            base_params["sourceTitle"] = source_title

        events: list[HistoryEvent] = []
        page = 1
        while True:
            data = await self._get("/api/v3/history", params={**base_params, "page": page})
            raw_records = data.get("records", [])
            for raw in raw_records:
                events.append(
                    HistoryEvent(
                        event_type=raw.get("eventType"),
                        download_id=raw.get("downloadId"),
                        source_title=raw.get("sourceTitle"),
                        raw=raw,
                    )
                )
            total = data.get("totalRecords", len(raw_records))
            # Same "trust the running count, not page * PAGE_SIZE" reasoning as
            # `queue_records` above.
            if not raw_records or len(events) >= total:
                break
            page += 1
        return events

    async def post_scan_command(self, path: str) -> dict[str, Any]:
        """`POST /api/v3/command` -- "your files are here, import now"
        (docs/arr-integration-spec.md "Notify"). `importMode: "Copy"` deliberately, per the
        spec's own reasoning: a `Move` import would rip files out from under lftpweb's tracking
        mid-flight, and Copy keeps "who deletes what" in exactly one place (our own cleanup
        step, phase B).

        `path` must already be in the *arr's own namespace (`path_queue.arr_visible_path`'s
        translation, spec "Path namespaces") -- this client does no path translation of its own.

        **The response body's own `id` is not decoration** (2026-08-17, scan-command outcome
        verification): a 201 here only means "command queued", not "the *arr could act on this
        path" -- `core/arrnotify.py.notify_arr` persists this `id` (`item.arr_scan_command_id`,
        migration 021) so `get_command` below can poll for the outcome on a later pass.
        """
        name = _SCAN_COMMAND_NAME[self.kind]
        try:
            response = await self._client.post(
                "/api/v3/command",
                json={"name": name, "path": path, "importMode": "Copy"},
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise ArrClientError(f"{self.kind} command {name} failed: {exc}") from exc
        return response.json()

    async def get_command(self, command_id: int) -> dict[str, Any] | None:
        """`GET /api/v3/command/{id}` -- the eventual outcome of a previously-pushed command
        (2026-08-17, scan-command outcome verification: `post_scan_command`'s 201 is otherwise
        fire-and-forget). `core/arrsync.py`'s poller calls this on later passes for every item
        still carrying a `item.arr_scan_command_id`.

        `None` on a 404 -- the *arr prunes finished commands after a while, and a restarted
        instance loses its in-memory command history entirely, so an unknown id is a routine,
        expected outcome, not a failure. Every caller treats it as "no evidence either way,"
        the same reading `import_events`'s empty-list case gets when there's nothing to say.
        Any other non-2xx still raises `ArrClientError`, same as every other method here.
        """
        try:
            response = await self._client.get(f"/api/v3/command/{command_id}")
        except httpx.HTTPError as exc:
            raise ArrClientError(
                f"{self.kind} GET /api/v3/command/{command_id} failed: {exc}"
            ) from exc
        if response.status_code == 404:
            return None
        try:
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise ArrClientError(
                f"{self.kind} GET /api/v3/command/{command_id} failed: {exc}"
            ) from exc
        return response.json()


CommandOutcome = Literal["completed", "failed", "pending"]


def command_outcome(raw: dict[str, Any]) -> CommandOutcome:
    """Classify a `/api/v3/command/{id}` response body's `status`
    (docs/arr-integration-spec.md doesn't cover this endpoint, and -- unlike `eventType`/
    `trackedDownloadState` above -- this vocabulary is **not** yet verified against a live
    instance; it follows the *arr v3 API's own public documentation instead. See
    docs/decisions.md 2026-08-17 for the full reasoning if a live instance is ever found to
    disagree). `"completed"` and `"failed"` are the only statuses the *arr is documented to
    ever settle on; anything else (`"queued"`, `"started"`, a value this codebase hasn't seen)
    reads as still in flight -- the safe default, since the poller simply checks again next
    pass rather than guessing at a terminal outcome.
    """
    status = str(raw.get("status") or "").lower()
    if status == "failed":
        return "failed"
    if status == "completed":
        return "completed"
    return "pending"
