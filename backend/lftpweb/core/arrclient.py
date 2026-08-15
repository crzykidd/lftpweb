"""Sonarr/Radarr v3 API client (docs/arr-integration-spec.md "Scope"/"The poller").

One async class over `httpx`, not two -- the two APIs are shape-identical for everything this
codebase touches (`system/status`, `queue`, `history`, `command`); only the command *name* and
media noun differ, so `kind` is a constructor argument, not a subclass split. `X-Api-Key` header,
10s timeout, matching `docs/arr-integration-spec.md`'s "The poller" section verbatim.

**The numeric `eventType` codes and the `trackedDownloadState` string vocabulary below are
data-driven constants sourced from the public v3 API's community documentation, not confirmed
against a live Sonarr/Radarr instance** (this build had none available) -- the spec's own
"Failure modes" section flags this explicitly, the same lesson
`tests/test_lftp_settings_accepted.py` teaches for lftp's rc grammar: assert against the real
program, not the docs, at the first opportunity a live instance exists. Until then, everything
that reads `eventType`/`trackedDownloadState` reads it through the two module-level lookups
below so a correction is a one-place edit.
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

# *arr v3 history `eventType` values -- **unverified against a live instance, see module
# docstring.** `downloadFolderImported` (3) is the one this codebase cares about: it is the
# per-file trailing signal a completed import leaves behind (docs/arr-integration-spec.md "The
# association lifecycle" -- "one history import event lands *after each file's copy
# completes*"). `grabbed` (1) and `downloadFailed` (4) are recorded here for completeness/future
# use, not read by `core/arrsync.py` in this phase.
EVENT_TYPE_GRABBED = 1
EVENT_TYPE_DOWNLOAD_FOLDER_IMPORTED = 3
EVENT_TYPE_DOWNLOAD_FAILED = 4

# The only event type(s) that count as "this release was imported" for the lifecycle's
# requirement 2 (docs/arr-integration-spec.md). A `frozenset` (not a single constant) because a
# future correction against a live instance may find more than one event type qualifies (e.g. a
# per-episode vs. per-season variant) without every caller changing shape.
IMPORT_EVENT_TYPES: frozenset[int] = frozenset({EVENT_TYPE_DOWNLOAD_FOLDER_IMPORTED})

# *arr v3 queue record `trackedDownloadState` vocabulary -- **unverified against a live
# instance, see module docstring.** `importing` is the value the spec's lifecycle section leans
# on directly: a record still reporting it is *never* `imported`, no matter what history says
# (requirement 1's "not yet" case for a slow multi-file import).
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
    """One `/api/v3/history` record -- same "narrow projection + raw" shape as `QueueRecord`."""

    event_type: int | None
    download_id: str | None
    source_title: str | None
    raw: dict[str, Any] = field(repr=False)


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

        Not called by anything yet in this phase -- built now, per the handoff prompt, so the
        client is complete before phase B wires it into `PostprocessPipeline.process_item`'s
        tail. `path` must already be in the *arr's own namespace
        (`path_queue.arr_visible_path`'s translation, spec "Path namespaces") -- this client
        does no path translation of its own.
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
