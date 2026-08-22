"""The SABnzbd connector (docs/download-client-framework-spec.md §14 stage 1) -- the first real
adapter against the stage 0 framework (`core/clients/base.py`).

**Every status-mapping table, every field-to-endpoint pairing, and every parsing choice in this
module is authored from vendor documentation (sabnzbd.org/wiki/configuration/5.1/api,
2026-08-22) and is UNVERIFIED against a live SABnzbd instance.** This repo has already shipped
a defect of exactly this shape: `core/arrclient.py`'s `IMPORT_EVENT_TYPES = {3}` was a
plausible-looking numeric guess that was simply wrong, and the fake *arr fixture built to test
it encoded the identical wrong guess, so every test stayed green while two live Sonarr imports
were silently misclassified `gone` (see that module's own docstring, and
docs/decisions.md). The discipline this module follows to avoid repeating that: every doc-derived
constant below says so in its own comment, and every genuinely ambiguous vendor-doc reading
prefers the tolerant answer (`TransferPhase.UNKNOWN`, `None`) over a confident guess -- an
unknown phase costs nothing downstream; a wrong one can silently block work (spec §3, §4.2).
Once spec §13.3's capture (`core.clients.capture`) has real bytes from a live instance (stage
1b), every marker below is the list of things to go correct against them.

**Auth is an `apikey` query parameter, plus `output=json`** (spec: "the thing §13.3's redaction
exists for -- the secret is in the URL, so it lands in any naive log line"). `test_connection`
is the one method that actually exercises the capture helper end to end, for exactly that
reason (spec §13.3: "a client that will not connect becomes diagnosable from the log rather
than by guesswork").

**Two sources, per spec §2.1**: `mode=queue` (in-flight) and `mode=history` (finished/failed).
`list_transfers(active_only=True)` reads only the queue; the full form
(`active_only=False`, and `list_history`'s own dedicated call) also reads history, which
carries the `storage` field -- the real on-disk path after rename and unpack, and the only
trustworthy `content_path` source (spec §7.2: never predict a path from a release name).

Follows `core/arrclient.py`'s house style: one `httpx.AsyncClient` built at construction time
(spec's own reading of "constructed per use" -- one per connector instance's lifetime, matching
`ArrClient.__init__`, not a fresh client per call) with a 10s timeout, narrow-projection
dataclasses, and dense docstrings that say *why*.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

import httpx

from . import register_client
from .base import (
    Capability,
    CapabilitySet,
    ConfigField,
    DownloadClient,
    Field,
    Support,
    USENET_BASELINE,
    project_transfer,
)
from .capture import capture_response
from .errors import CapabilityUnavailable, ClientError, ClientUnreachable
from .models import (
    BasePath,
    BasePathKind,
    ConnectionInfo,
    RemoveOutcome,
    SpaceInfo,
    Transfer,
    TrackerInfo,
    TransferPhase,
    normalize_client_id,
)

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S = 10.0

# --------------------------------------------------------------------------------------------
# `map_phase` (spec §3) -- **doc-derived, UNVERIFIED against a live SABnzbd, 2026-08-22.**
#
# SABnzbd's real, documented queue-slot `status` values (from vendor docs and the values named
# directly in this stage's own handoff prompt): `Queued`, `Downloading`, `Paused`, `Grabbing`
# (fetching the .nzb itself, before any content download starts), `Fetching` (fetching
# additional par2 repair blocks mid-repair), `Verifying`/`QuickCheck`/`Checking` (par2
# integrity check), `Repairing` (par2 repair using those blocks), `Extracting` (unpacking
# archives), `Moving` (relocating finished files into the configured complete folder),
# `Running` (a post-processing user script), and -- on newer SABnzbd versions only, per vendor
# docs -- `Propagating` (a settle delay before the par2 check begins).
#
# None of these ever appear as a *terminal* verdict in the queue -- a finished or failed item
# leaves the queue entirely and appears in history instead, so every mapping below is to a
# non-terminal `TransferPhase`; the finer distinction between e.g. `Repairing` and `Verifying`
# is genuinely a judgment call from the vendor docs, not a verified fact, but getting a
# non-terminal status wrong (mapping it to another non-terminal phase) is a materially lower
# stakes mistake than a terminal/non-terminal confusion, since spec §4.2's "unknown never
# blocks anything" already covers this connector against the higher-stakes error.
_QUEUE_PHASE_MAP: dict[str, TransferPhase] = {
    "queued": TransferPhase.QUEUED,
    # "Grabbing" is fetching the .nzb file itself from an indexer/URL, before the item is even
    # a real download yet -- closer to "not started" than "actively transferring."
    "grabbing": TransferPhase.QUEUED,
    "downloading": TransferPhase.DOWNLOADING,
    # "Fetching" (additional par2 blocks mid-repair) and "Propagating" (a post-download settle
    # delay before the par2 check) are both part of the post-download verify/repair sequence in
    # vendor docs, not the primary content download -- mapped alongside "Verifying" rather than
    # "Downloading" on that reading.
    "fetching": TransferPhase.VERIFYING,
    "propagating": TransferPhase.VERIFYING,
    "paused": TransferPhase.PAUSED,
    "verifying": TransferPhase.VERIFYING,
    "quickcheck": TransferPhase.VERIFYING,
    "checking": TransferPhase.VERIFYING,
    "repairing": TransferPhase.VERIFYING,
    "extracting": TransferPhase.EXTRACTING,
    # "Moving" (relocating finished files into the complete folder) and "Running" (a
    # post-processing script) have no dedicated `TransferPhase` of their own; both are
    # post-extraction tidy-up steps, so both are grouped with `EXTRACTING` rather than invented
    # a phase the rest of this codebase doesn't have.
    "moving": TransferPhase.EXTRACTING,
    "running": TransferPhase.EXTRACTING,
}

# History `status` -- **doc-derived, UNVERIFIED.** SABnzbd's history vocabulary is much
# smaller and, per vendor docs, only ever settles on one of these two terminal values.
_HISTORY_PHASE_MAP: dict[str, TransferPhase] = {
    "completed": TransferPhase.COMPLETED,
    "failed": TransferPhase.FAILED,
}


def _map_phase(raw_status: str) -> TransferPhase:
    key = raw_status.strip().lower()
    phase = _QUEUE_PHASE_MAP.get(key)
    if phase is None:
        phase = _HISTORY_PHASE_MAP.get(key)
    return phase if phase is not None else TransferPhase.UNKNOWN


def _mb_string_to_bytes(value: Any) -> int | None:
    """SABnzbd's queue slot reports size in MB as a numeric string (`"mb"`/`"mbleft"`) --
    doc-derived, UNVERIFIED. Tolerant: an unparseable or missing value is `None`, never a raise
    -- a connector-level parsing quirk must not turn into a hard failure of the whole call
    (spec's own "prefer the tolerant reading" instruction).
    """
    try:
        return round(float(value) * 1024 * 1024)
    except (TypeError, ValueError):
        return None


def _gb_string_to_bytes(value: Any) -> int | None:
    """The queue response's `diskspace*`/`diskspacetotal*` fields -- doc-derived, UNVERIFIED,
    as GB-denominated numeric strings. Same tolerant-`None` reasoning as `_mb_string_to_bytes`.
    """
    try:
        return round(float(value) * 1024 * 1024 * 1024)
    except (TypeError, ValueError):
        return None


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_timeleft(value: Any) -> int | None:
    """SABnzbd's queue slot `timeleft` -- doc-derived, UNVERIFIED, as an `"H:MM:SS"` string.
    Tolerant of anything else (`None`, an unexpected shape) -- returns `None` rather than
    raising, exactly like the byte parsers above.
    """
    if not isinstance(value, str):
        return None
    parts = value.split(":")
    if len(parts) != 3:
        return None
    try:
        hours, minutes, seconds = (int(p) for p in parts)
    except ValueError:
        return None
    return hours * 3600 + minutes * 60 + seconds


def _epoch_to_iso(value: Any) -> str | None:
    """History's `completed` field -- doc-derived, UNVERIFIED, as a Unix epoch integer.
    Tolerant of a missing/malformed value, same reasoning as the parsers above.
    """
    try:
        return datetime.fromtimestamp(int(value), tz=UTC).isoformat()
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def _sab_call_ok(data: Any) -> bool:
    """SABnzbd's action endpoints (`name=pause`/`resume`/`delete`/`change_cat`) -- doc-derived,
    UNVERIFIED -- typically answer `{"status": true}` on success. Tolerant of a bare boolean
    body too, in case a given action call's real shape differs from the object form; anything
    else reads as "not confirmed successful" rather than raising, since this is used to decide
    between two fallback code paths (`remove`), not to raise an error of its own.
    """
    if isinstance(data, bool):
        return data
    if isinstance(data, dict):
        return bool(data.get("status"))
    return False


@register_client("sabnzbd")
class SabnzbdClient(DownloadClient):
    """SABnzbd (spec §14 stage 1) -- a usenet client, not a torrent client (spec §5): no
    ratio, seed time, or trackers, and `recheck` (verifying data against a torrent) has no
    usenet analogue. Starts from `USENET_BASELINE` and overrides exactly one field (see
    `capabilities` below), matching spec §5's "a connector author writes ~3 lines instead of
    ~25."
    """

    family = "usenet"

    # `USENET_BASELINE` already covers everything this connector can genuinely populate, with
    # one exception: **`Field.ADDED_AT` is overridden to `Support.NONE`** here, deliberately
    # departing from the baseline. Doc-derived, UNVERIFIED, 2026-08-22: neither `mode=queue`
    # nor `mode=history`, per vendor docs, appears to expose a "when was this added/queued"
    # timestamp (`mode=history`'s own `completed` field is the *finish* time, not the start).
    # Declaring `ADDED_AT` `NATIVE` and then simply never setting it would be exactly the
    # mistake spec §2.2 warns against ("a field declared and returned `None` is worse than one
    # declared absent"), so it is declared unsupported instead, pending confirmation from a
    # live capture (spec §13.3) that SABnzbd genuinely has no such field.
    capabilities: CapabilitySet = USENET_BASELINE.overridden(
        fields={
            Field.ADDED_AT: Capability(
                Support.NONE,
                note=(
                    "doc-derived, UNVERIFIED 2026-08-22: neither mode=queue nor mode=history "
                    "appears to expose an added/queued timestamp in vendor docs"
                ),
            ),
        },
    )

    config_schema = (
        ConfigField(
            key="base_url",
            label="Base URL",
            kind="str",
            help_text=(
                "e.g. http://seedbox:8080, or http://seedbox:8080/sabnzbd if SABnzbd is "
                "mounted under a path. The API is always at <base URL>/api."
            ),
        ),
        ConfigField(
            key="api_key",
            label="API key",
            kind="secret",
            help_text="Settings -> General -> API Key in SABnzbd's own web UI.",
        ),
    )

    def __init__(self, *, config: dict[str, Any]) -> None:
        super().__init__(config=config)
        base_url = str(config["base_url"]).rstrip("/")
        self._api_key = str(config["api_key"])
        self._client = httpx.AsyncClient(base_url=base_url, timeout=DEFAULT_TIMEOUT_S)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> SabnzbdClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    @staticmethod
    def map_phase(raw_status: str) -> TransferPhase:
        return _map_phase(raw_status)

    # ------------------------------------------------------------------------------------
    # Transport -- the one place the three-way error taxonomy (spec §4.2) is actually drawn.
    # ------------------------------------------------------------------------------------

    async def _get(self, mode: str, **params: Any) -> tuple[httpx.Response, Any]:
        """One `GET /api?mode=...` round trip. Raises `ClientUnreachable` for a transport-level
        failure (DNS, connection refused, timeout -- says nothing about what the client
        *supports*), `ClientError` for anything the client actually answered but that this
        connector cannot treat as success (a non-2xx status, a non-JSON body, or SABnzbd's own
        documented `{"status": false, "error": "..."}` failure shape, which vendor docs describe
        SABnzbd returning with an HTTP 200 rather than a 4xx/5xx). **Never `CapabilityUnavailable`
        from this method** -- none of these failure modes are the client explicitly saying "I
        cannot do this" (spec §4.2); that type is reserved for the two statically-declared-`NONE`
        operations below (`list_trackers`, `recheck`), raised without ever reaching the network.

        **`{"status": false}` alone, with no `error` key, is not treated as a failure here --
        doc-derived, UNVERIFIED, 2026-08-22.** The action endpoints (`name=delete` in
        particular) answer `{"status": false}` for a perfectly routine outcome -- "this id was
        not found here" -- which `remove`'s own queue-then-history fallback depends on being
        able to read as data, not as a raised error. Only a body that also carries a truthy
        `error` reads as this call having failed; a caller that needs the bare `status` value
        (`remove`, via `_sab_call_ok`) reads it from the returned `data` itself.
        """
        query = {"mode": mode, "output": "json", "apikey": self._api_key, **params}
        try:
            response = await self._client.get("/api", params=query)
        except httpx.TransportError as exc:
            raise ClientUnreachable(f"sabnzbd {mode} unreachable: {exc}") from exc
        except httpx.HTTPError as exc:
            raise ClientError(f"sabnzbd {mode} failed: {exc}") from exc
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise ClientError(f"sabnzbd {mode} returned HTTP {response.status_code}") from exc
        try:
            data = response.json()
        except ValueError as exc:
            raise ClientError(f"sabnzbd {mode} returned a non-JSON body") from exc
        if isinstance(data, dict) and data.get("status") is False and data.get("error"):
            raise ClientError(f"sabnzbd {mode} reported an error: {data.get('error')!r}")
        return response, data

    async def _get_queue(self) -> dict[str, Any]:
        _, data = await self._get("queue")
        queue = data.get("queue") if isinstance(data, dict) else None
        return queue if isinstance(queue, dict) else {}

    async def _get_history(self) -> dict[str, Any]:
        _, data = await self._get("history")
        history = data.get("history") if isinstance(data, dict) else None
        return history if isinstance(history, dict) else {}

    # ------------------------------------------------------------------------------------
    # Normalization -- queue slot / history slot -> `Transfer`.
    # ------------------------------------------------------------------------------------

    def _transfer_from_queue_slot(self, slot: dict[str, Any]) -> Transfer:
        client_id = normalize_client_id(str(slot.get("nzo_id") or ""))
        raw_status = str(slot.get("status") or "")
        size_bytes = _mb_string_to_bytes(slot.get("mb"))
        left_bytes = _mb_string_to_bytes(slot.get("mbleft"))
        bytes_done = None
        if size_bytes is not None and left_bytes is not None:
            bytes_done = max(size_bytes - left_bytes, 0)
        return Transfer(
            client_id=client_id,
            name=str(slot.get("filename") or ""),
            phase=self.map_phase(raw_status),
            raw_status=raw_status,
            raw=slot,
            size_bytes=size_bytes,
            bytes_done=bytes_done,
            eta_s=_parse_timeleft(slot.get("timeleft")),
            category=slot.get("cat") or None,
        )

    def _transfer_from_history_slot(self, slot: dict[str, Any]) -> Transfer:
        client_id = normalize_client_id(str(slot.get("nzo_id") or ""))
        raw_status = str(slot.get("status") or "")
        # History carries the real on-disk path after rename/unpack in `storage` -- spec §7.2:
        # never predict a path from a release name; this is the only trustworthy source.
        content_path = slot.get("storage") or None
        size_bytes = _int_or_none(slot.get("bytes"))
        # `fail_message` is the explicit-failure signal spec §4.2 turns on -- an empty string
        # (SABnzbd's own "no failure" shape, per vendor docs) reads as `None`, never as an
        # empty-but-truthy error, so a caller checking `error_message is not None` sees a real
        # signal only when there genuinely is one.
        fail_message = slot.get("fail_message") or None
        return Transfer(
            client_id=client_id,
            name=str(slot.get("name") or ""),
            phase=self.map_phase(raw_status),
            raw_status=raw_status,
            raw=slot,
            content_path=content_path,
            size_bytes=size_bytes,
            bytes_done=size_bytes,
            error_message=fail_message,
            category=slot.get("category") or None,
            completed_at=_epoch_to_iso(slot.get("completed")),
        )

    # ------------------------------------------------------------------------------------
    # `DownloadClient` interface.
    # ------------------------------------------------------------------------------------

    async def test_connection(self) -> ConnectionInfo:
        query = {"mode": "version", "output": "json", "apikey": self._api_key}
        try:
            response = await self._client.get("/api", params=query)
        except httpx.TransportError as exc:
            raise ClientUnreachable(f"sabnzbd unreachable: {exc}") from exc
        except httpx.HTTPError as exc:
            raise ClientError(f"sabnzbd test_connection failed: {exc}") from exc
        # Captured (redacted) before anything else is done with the response -- spec §13.3:
        # "redaction happens at the point of capture, never later, before display." The request
        # URL is included on purpose: the API key rides the query string (spec's own reasoning
        # for why this helper exists at all), so a naive log of the URL alone would leak it.
        raw_sample = f"GET {response.request.url}\n{response.text}"
        logger.debug(
            "sabnzbd test_connection response: %s",
            capture_response(raw_sample, secrets=(self._api_key,)),
        )
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise ClientError(
                f"sabnzbd test_connection returned HTTP {response.status_code}"
            ) from exc
        try:
            data = response.json()
        except ValueError as exc:
            raise ClientError("sabnzbd test_connection returned a non-JSON body") from exc
        if isinstance(data, dict) and data.get("status") is False and data.get("error"):
            raise ClientError(f"sabnzbd test_connection reported an error: {data.get('error')!r}")
        version = data.get("version") if isinstance(data, dict) else None
        return ConnectionInfo(version=version, raw=data if isinstance(data, dict) else {})

    async def list_transfers(self, *, active_only: bool = False) -> list[Transfer]:
        queue = await self._get_queue()
        transfers = [self._transfer_from_queue_slot(s) for s in queue.get("slots", []) or []]
        if not active_only:
            history = await self._get_history()
            transfers += [
                self._transfer_from_history_slot(s) for s in history.get("slots", []) or []
            ]
        return [project_transfer(t, self.capabilities) for t in transfers]

    async def list_history(self) -> list[Transfer]:
        history = await self._get_history()
        transfers = [self._transfer_from_history_slot(s) for s in history.get("slots", []) or []]
        return [project_transfer(t, self.capabilities) for t in transfers]

    async def get_transfer(self, client_id: str) -> Transfer | None:
        # Derived (spec §5's `USENET_BASELINE`: "filter the list") -- SABnzbd may well support a
        # native single-id lookup, unconfirmed (spec §5's own caveat on this exact capability);
        # filtering the merged list is the safe, doc-independent implementation either way.
        wanted = normalize_client_id(client_id)
        for transfer in await self.list_transfers(active_only=False):
            if transfer.client_id == wanted:
                return transfer
        return None

    async def list_trackers(self, client_id: str) -> list[TrackerInfo]:
        # Statically declared `Support.NONE` (`USENET_BASELINE`: "usenet has no trackers") --
        # raised immediately, never reaching the network, matching the ABC's own suggested
        # pattern (`DownloadClient.recheck`'s docstring) for a declared-unsupported operation.
        raise CapabilityUnavailable("sabnzbd has no tracker concept (usenet, not torrent)")

    async def list_files(self, client_id: str) -> list[str]:
        # Doc-derived, UNVERIFIED: `mode=get_files&value=<nzo_id>` per vendor docs. Tolerant of
        # either a bare list or an `{"files": [...]}` wrapper, since the exact response shape is
        # unconfirmed; either way, a filename-less or unexpected entry is skipped rather than
        # raising.
        _, data = await self._get("get_files", value=client_id)
        if isinstance(data, list):
            candidates = data
        elif isinstance(data, dict):
            candidates = data.get("files") or []
        else:
            candidates = []
        return [
            str(entry["filename"])
            for entry in candidates
            if isinstance(entry, dict) and entry.get("filename")
        ]

    async def list_base_paths(self) -> list[BasePath]:
        # Doc-derived, UNVERIFIED: `mode=get_config&section=misc` per vendor docs, reading
        # `complete_dir`/`download_dir`. Detected, not saved (spec §8.2 correction) -- the role
        # (`kind`) is known because this connector knows which config key it read each path
        # from; whether lftpweb can see either path at the same spot over SSH is a separate
        # question `core.clients.detection` answers, not this method. Missing/absent fields
        # simply contribute nothing rather than raising.
        _, data = await self._get("get_config", section="misc")
        misc = {}
        if isinstance(data, dict):
            config = data.get("config")
            if isinstance(config, dict):
                candidate = config.get("misc")
                if isinstance(candidate, dict):
                    misc = candidate
        paths: list[BasePath] = []
        complete_dir = misc.get("complete_dir")
        if complete_dir:
            paths.append(BasePath(path=str(complete_dir), kind=BasePathKind.CONTENT))
        download_dir = misc.get("download_dir")
        if download_dir:
            paths.append(BasePath(path=str(download_dir), kind=BasePathKind.WORKING))
        return paths

    async def free_space(self, path: str) -> SpaceInfo:
        """`path` is accepted for interface conformance but not used to select a value --
        SABnzbd's `mode=queue` response exposes exactly two numbered pairs (`diskspace1`/
        `diskspacetotal1`, `diskspace2`/`diskspacetotal2`) with **no path association** in
        vendor docs, so there is nothing to match `path` against. **Doc-derived, UNVERIFIED,
        2026-08-22**: vendor docs describe `diskspace1` as the temporary/incomplete download
        folder and `diskspace2` as the complete download folder; this reads `diskspace2`/
        `diskspacetotal2` on that reading, since the complete folder is the one lftpweb's own
        base paths (spec §8.2) actually care about. Disk space rides the queue call itself
        (spec's own survey note) -- no second request is issued for this.
        """
        queue = await self._get_queue()
        free_gb = queue.get("diskspace2")
        if free_gb is None:
            raise ClientError("sabnzbd queue response is missing diskspace2")
        free_bytes = _gb_string_to_bytes(free_gb)
        if free_bytes is None:
            raise ClientError(f"sabnzbd reported a non-numeric diskspace2 value: {free_gb!r}")
        total_bytes = _gb_string_to_bytes(queue.get("diskspacetotal2"))
        return SpaceInfo(free_bytes=free_bytes, total_bytes=total_bytes)

    async def pause(self, client_id: str) -> None:
        await self._get("queue", name="pause", value=client_id)

    async def resume(self, client_id: str) -> None:
        await self._get("queue", name="resume", value=client_id)

    async def remove(self, client_id: str) -> RemoveOutcome:
        """Unregister the item, **leave the data on disk** (spec §2.1, §10.1) -- the only
        removal verb this vocabulary has. **`del_files=0` is passed explicitly on every delete
        call, never omitted or left to whatever SABnzbd's own default is** -- this is the
        single safety-critical line in this method (spec §10.1: `remove` must never delete
        data). Tries the queue first (an in-flight item), then falls back to history (a
        finished/failed item) -- `remove` is not told in advance which side a given id lives on.
        """
        _, queue_result = await self._get("queue", name="delete", value=client_id, del_files="0")
        if _sab_call_ok(queue_result):
            return RemoveOutcome(succeeded=True, detail="removed from queue")
        _, history_result = await self._get(
            "history", name="delete", value=client_id, del_files="0"
        )
        if _sab_call_ok(history_result):
            return RemoveOutcome(succeeded=True, detail="removed from history")
        return RemoveOutcome(succeeded=False, detail="nzo_id not found in queue or history")

    async def set_label(self, client_id: str, label: str) -> None:
        await self._get("queue", name="change_cat", value=client_id, value2=label)

    async def recheck(self, client_id: str) -> None:
        # Statically declared `Support.NONE` (`USENET_BASELINE`: torrent-only operation) --
        # same immediate-raise pattern as `list_trackers` above.
        raise CapabilityUnavailable("sabnzbd has no recheck concept (usenet, not torrent)")
