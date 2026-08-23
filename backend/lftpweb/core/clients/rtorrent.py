"""The rTorrent connector (docs/download-client-framework-spec.md §14 stage 1/2, GitHub #21) --
the second real adapter against the stage 0 framework (`core/clients/base.py`), and the first
one to exercise `TORRENT_BASELINE`.

**Endpoint, MEASURED against a real seedbox, 2026-08-22** (see
`prompts/2026-08-22-rtorrent-connector.md`): all four candidate URLs sit behind HTTP Basic auth.
`/RPC2` answered 401 with `WWW-Authenticate: Basic realm="ruTorrent Private Area"` -- **direct
XML-RPC, the chosen default**, made a `ConfigField` (`rpc_path`) so a deployment can point at
`/xmlrpc` or a ruTorrent plugin mount instead. `/rutorrent/plugins/httprpc/action.php` answered
the same realm (ruTorrent's own plugin, not chosen as the default). `/xmlrpc` answered 401 with a
**different** realm (`"Private Area"`) -- a separate nginx location, not assumed to be the same
backend. `/rutorrent/plugins/rpc/rpc.php` answered **404** -- confirmed absent. This settles
spec §15's open question #4: `docs/torrent-manager-spec.md` §10.1's "direct XML-RPC or the
ruTorrent HTTP plugin" is answered *direct*, and is now cheap to reverse because §10's SSH-based
deletion removed the `erasedata` plugin from the critical path entirely -- the only thing that
made the endpoint choice consequential in the first place.

**Everything past that 401 is vendor-doc guesswork -- there are no credentials for the live
instance.** This repo has been bitten *twice* by a test fixture that encodes the same wrong
assumption as the code it tests (`core/arrclient.py`'s `IMPORT_EVENT_TYPES = {3}`, which reached
production; and SABnzbd's auth shape, caught by a user on day one, GitHub #23 -- see
`sabnzbd.py`'s own module docstring and spec §13.4). The discipline this module follows to avoid
a third occurrence: every doc-derived mapping and constant says so in its own comment and is
listed in spec **§13.6** (the correction list this connector adds, mirroring §13.4's SABnzbd
list); every genuinely ambiguous reading prefers the tolerant answer (`TransferPhase.UNKNOWN`,
`None`, a raised `ClientError` rather than a silently-wrong success) over a confident guess.
`tests/fake_rtorrent.py` inherits every one of these guesses -- see its own docstring -- so a
green suite here proves internal consistency with this module's own reading of the vendor docs,
not correctness against a real rTorrent.

**Transport: `httpx` + stdlib `xmlrpc.client.dumps()`/`loads()`, never `xmlrpc.client.
ServerProxy`** (that class is synchronous and would block the event loop) -- one POST per call,
`Content-Type: text/xml`, HTTP Basic auth via httpx's own `auth=`. No new runtime dependency.

**`remove` unregisters only -- `d.stop` then `d.erase`, nothing else.** `docs/
download-client-api-survey.md` §2's `d.custom5.set` -> `d.delete_tied` -> `d.erase` sequence
(the `erasedata` plugin hook) is **never implemented here, on purpose** -- spec §10.1 removed it
from the design entirely; lftpweb deletes bytes over SSH as a separate step outside any
connector. There is no code path in this module that can write `d.custom5` or call
`d.delete_tied`, not merely a convention against calling them.

**`raw_status` has no vendor word to preserve -- rTorrent's own status is spread across four
flags** (`d.hashing`, `d.complete`, `d.is_active`, `d.state`), not one string. The decision taken
here (recorded in full in `docs/decisions.md`, 2026-08-22): synthesize a single lowercase token
from exactly those flags -- `"hashing"` / `"seeding"` / `"completed"` / `"downloading"` /
`"paused"` / `"queued"` -- which is also, deliberately, the *same* vocabulary `map_phase` maps
from. This is not circular: rTorrent has no independent "display word" for `map_phase` to
translate, so the most honest synthesis *is* the same classification a human support view would
want to see next to the phase it produced -- never inventing a fifth vocabulary (a fake
SAB-style capitalized phrase) that would look like a real vendor string and isn't one.
"""

from __future__ import annotations

import logging
import xmlrpc.client
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

import httpx

from . import register_client
from .base import (
    Capability,
    CapabilitySet,
    ConfigField,
    DownloadClient,
    Field,
    Support,
    TORRENT_BASELINE,
    project_transfer,
)
from .capture import capture_response
from .errors import (
    CapabilityUnavailable,
    ClientAuthenticationFailed,
    ClientError,
    ClientUnreachable,
)
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
DEFAULT_RPC_PATH = "/RPC2"

# --------------------------------------------------------------------------------------------
# `raw_status` synthesis / `map_phase` (spec §3, prompt's own "the interesting decision") --
# **doc-derived, UNVERIFIED against a live rTorrent, 2026-08-22.** See module docstring and
# `docs/decisions.md` for the full reasoning; this is only the mechanics.
#
# `_classify_token` is total over the four rTorrent flags this connector reads (hashing,
# complete, is_active, state) -- it never raises and always returns one of the six tokens below.
# It is the *only* place phase logic lives; both `raw_status` and `phase` are produced by running
# the same flags through it once, so the two can never disagree with each other for a single
# torrent.
#
# The PAUSED-vs-QUEUED split (incomplete + inactive) is this module's own elaboration on top of
# the prompt's simpler "incomplete + inactive -> PAUSED/QUEUED" guidance, using `d.state` (has
# the download been `d.start`ed at all) as a tiebreaker `d.is_active` alone doesn't give:
# `state=0` (never started, or explicitly `d.stop`ped) reads as PAUSED; `state=1` but not active
# (started, but currently not transferring -- no peers, tracker unreachable, throttled) reads as
# QUEUED. **Both are non-terminal phases**, and spec §4.2's "unknown never blocks anything" means
# a wrong split here costs nothing a caller would act on differently -- the risk this guess
# carries is materially lower than a terminal/non-terminal confusion, which is exactly why it is
# attempted here rather than left as `TransferPhase.UNKNOWN` (spec §13.6 #2 tracks it anyway).
_RTORRENT_STATUS_MAP: dict[str, TransferPhase] = {
    "hashing": TransferPhase.VERIFYING,
    "seeding": TransferPhase.SEEDING,
    "completed": TransferPhase.COMPLETED,
    "downloading": TransferPhase.DOWNLOADING,
    "paused": TransferPhase.PAUSED,
    "queued": TransferPhase.QUEUED,
}


def _classify_token(*, hashing: bool, complete: bool, is_active: bool, state_started: bool) -> str:
    """Total: always returns one of `_RTORRENT_STATUS_MAP`'s six keys. `hashing` overrides every
    other flag on the doc-derived reading that a torrent can be re-verifying data regardless of
    its completion state (an integrity recheck), and that fact is more useful to a caller than
    whatever the completion/activity flags happened to say at the same moment.
    """
    if hashing:
        return "hashing"
    if complete:
        return "seeding" if is_active else "completed"
    if is_active:
        return "downloading"
    return "paused" if not state_started else "queued"


def _map_phase(raw_status: str) -> TransferPhase:
    return _RTORRENT_STATUS_MAP.get(raw_status, TransferPhase.UNKNOWN)


def _flag(value: Any) -> bool:
    """rTorrent's boolean-shaped fields (`d.complete`, `d.is_active`, `d.hashing`, `d.state`) --
    doc-derived, UNVERIFIED, as `0`/`1` integers over XML-RPC (`<int>`). Tolerant of a bare bool
    or a numeric string too, in case a given deployment's XML-RPC library marshals differently;
    anything unparseable reads as `False` rather than raising, same "prefer the tolerant answer"
    discipline as `sabnzbd.py`'s numeric parsers.
    """
    if isinstance(value, bool):
        return value
    try:
        return int(value) != 0
    except (TypeError, ValueError):
        return False


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _epoch_to_iso(value: Any) -> str | None:
    """`d.timestamp.started=` / `d.timestamp.finished=` -- doc-derived, UNVERIFIED, as Unix epoch
    integers, `0` meaning "not set" (never happened) rather than a real epoch-zero timestamp --
    same convention `sabnzbd.py`'s history parsing uses for its own epoch fields.
    """
    epoch = _int_or_none(value)
    if not epoch:
        return None
    try:
        return datetime.fromtimestamp(epoch, tz=UTC).isoformat()
    except (OSError, OverflowError, ValueError):
        return None


# --------------------------------------------------------------------------------------------
# The `d.multicall2` field list (spec's own explicit instruction: one round trip, not per-item
# calls) -- doc-derived, UNVERIFIED command names, 2026-08-22. Order matters: a multicall2 result
# row is a plain list of values in the same order these commands were requested, with no field
# names attached -- `_row_to_dict` below zips this list back onto them.
# --------------------------------------------------------------------------------------------
_LISTING_FIELDS: tuple[tuple[str, str], ...] = (
    ("hash", "d.hash="),
    ("name", "d.name="),
    ("size_bytes", "d.size_bytes="),
    ("completed_bytes", "d.completed_bytes="),
    ("left_bytes", "d.left_bytes="),
    ("down_rate", "d.down.rate="),
    ("up_total", "d.up.total="),
    ("ratio", "d.ratio="),
    ("state", "d.state="),
    ("complete", "d.complete="),
    ("is_active", "d.is_active="),
    ("hashing", "d.hashing="),
    ("message", "d.message="),
    # **Chosen over `d.directory=`, doc-derived, UNVERIFIED (prompt: "pick one, explain the
    # choice").** `d.base_path=` is the full path to the download's actual content -- for a
    # single-file torrent this includes the filename, where `d.directory=` would only give the
    # parent directory (misleading as a "content path" for that case). For a multi-file torrent
    # the two are documented as equivalent. Per spec §1.1 this is the *seeding* location, not the
    # hardlinked completed-folder copy -- that copy is invisible to rTorrent's own API by design,
    # which is precisely why spec §11.1b matches on inode rather than trusting this path alone.
    ("base_path", "d.base_path="),
    # **Doc-derived, UNVERIFIED, and flagged HIGH risk in spec §13.6.** `d.custom1` is a
    # general-purpose per-download user slot rTorrent core exposes with no built-in meaning --
    # ruTorrent's own label UI is documented (by convention, not a core rTorrent contract) to
    # store its label text there. Reading it costs nothing extra (it rides this same multicall),
    # but nothing here confirms *this* deployment's ruTorrent, if any, actually uses that
    # convention, or that nothing else on the seedbox writes into the same slot for an unrelated
    # purpose (`docs/download-client-api-survey.md` §2's own warning about `d.custom5`,
    # generalized to `d.custom1`).
    ("custom1", "d.custom1="),
    ("timestamp_started", "d.timestamp.started="),
    ("timestamp_finished", "d.timestamp.finished="),
)


def _row_to_dict(row: Any) -> dict[str, Any]:
    values = list(row) if isinstance(row, (list, tuple)) else []
    return {
        key: values[i] if i < len(values) else None for i, (key, _cmd) in enumerate(_LISTING_FIELDS)
    }


# A run of exactly 40 hex characters (spec §7.1) -- rTorrent's own hash case is MEASURED nowhere
# yet, but every source this repo has (the *arr's `arr_matched` events, docs/
# transfers-redesign-spec.md §4.4, and the prompt's own explicit "infohashes come back uppercase"
# instruction) points at uppercase. `normalize_client_id` lowercases on the way *in* (storage/
# comparison, spec §7.1); the reverse direction -- what this connector sends back to rTorrent in
# a per-item call -- is a *different* question the shared helper does not answer, and is exactly
# where the next trap lives (see `_to_rtorrent_hash` below).
def _to_rtorrent_hash(client_id: str) -> str:
    """Uppercase a client-supplied id before it is ever sent back into an rTorrent RPC call
    (`d.stop`, `d.erase`, `d.pause`, `d.resume`, `t.multicall`, `f.multicall2`,
    `d.free_diskspace`, `d.custom1.set`).

    **This is a genuine, easy-to-miss trap, not defensive boilerplate -- flagged HIGH risk in
    spec §13.6.** `normalize_client_id` (spec §7.1) lowercases an infohash on the way into
    lftpweb's own storage/comparison layer, so every `client_id` this connector's public methods
    receive from a caller (the poller, the delete pipeline) is lowercase. If rTorrent's own hash
    lookup is case-sensitive -- doc-derived, UNVERIFIED, but consistent with every source this
    module has for rTorrent reporting hashes uppercase -- sending a lowercased id straight back
    into `d.stop`/`d.erase`/etc. would silently fail to find the item on a deployment where case
    actually matters. Non-hex-shaped ids (should not occur for rTorrent, but kept tolerant the
    same way `normalize_client_id` is) pass through `.upper()` harmlessly.
    """
    return client_id.upper()


# --------------------------------------------------------------------------------------------
# XML-RPC fault classification -- doc-derived, UNVERIFIED, 2026-08-22. rTorrent's exact fault
# text for "no such command" versus "no such info-hash" is not confirmed against a live
# instance; the substrings below are this module's best reading of common rTorrent/xmlrpc-c
# fault phrasing, and are spec §13.6's highest-risk entries precisely because they decide
# `ClientError` vs `CapabilityUnavailable` (§4.2's load-bearing distinction).
# --------------------------------------------------------------------------------------------
_MISSING_METHOD_MARKERS = (
    "could not find command",
    "method not found",
    "unknown method",
    "not a valid command",
    "no such method",
)

_UNKNOWN_HASH_MARKERS = (
    "could not find info-hash",
    "could not find download",
    "not found",
    "unknown hash",
    "no such hash",
)


def _looks_like_missing_method(fault_string: str) -> bool:
    lowered = (fault_string or "").lower()
    return any(marker in lowered for marker in _MISSING_METHOD_MARKERS)


def _looks_like_unknown_hash(fault_string: str) -> bool:
    lowered = (fault_string or "").lower()
    return any(marker in lowered for marker in _UNKNOWN_HASH_MARKERS)


@register_client("rtorrent")
class RtorrentClient(DownloadClient):
    """rTorrent -- a torrent client (spec §5): starts from `TORRENT_BASELINE` and overrides three
    entries (see `capabilities` below), matching spec §5's "a connector author writes ~3 lines
    instead of ~25."
    """

    family = "torrent"

    # `TORRENT_BASELINE` already covers everything this connector can genuinely populate, with
    # three departures, all doc-derived and UNVERIFIED, 2026-08-22:
    #
    # - **`Field.ETA_S` overridden `NATIVE` -> `DERIVED`.** rTorrent has no `d.eta` field at all
    #   (unlike SABnzbd's `timeleft`, which is why `USENET_BASELINE`/`TORRENT_BASELINE` both
    #   claim `NATIVE` for this key) -- it is computed here as `d.left_bytes / d.down.rate`,
    #   which is `None` whenever the rate is zero (stalled, or not currently downloading). Spec
    #   §4.3's own caveat rule applies: this is an *estimate* that swings with the current
    #   instantaneous rate, not a value rTorrent itself reports.
    # - **`Field.SEED_TIME_S` overridden `NATIVE` -> `DERIVED`, with a note.** This is a
    #   correction of `TORRENT_BASELINE` itself, not merely an override of it: the baseline
    #   (`core/clients/base.py`) declares this key `NATIVE` with no note, but spec §4.3's own
    #   canonical worked example is *this exact field* -- "rTorrent has no seed-time field... a
    #   `derived` capability carries a note, because the semantics differ" -- and this
    #   connector's own handoff prompt repeats the instruction explicitly. Declaring it `NATIVE`
    #   here would be exactly the mistake spec §2.2 warns against in the other direction: a field
    #   whose value is real but whose *meaning* (wall-clock since completion, not "actually
    #   seeding" time -- a stopped torrent still accrues) a caller relying on `NATIVE` would never
    #   think to question. Flagged in spec §13.6 as a pre-existing baseline inconsistency worth
    #   fixing at the source, not merely worked around here.
    # - **`Field.CATEGORY` stays `NATIVE`** (inherited unchanged from `TORRENT_BASELINE`) reading
    #   `d.custom1=` -- kept `NATIVE` rather than downgraded to `NONE` because a populated-but-
    #   possibly-wrong label is more useful to spec §8.3's category -> queue mapping than an
    #   always-empty field, but this is the single guess this module trusts least (see
    #   `_LISTING_FIELDS`'s own comment and spec §13.6).
    capabilities: CapabilitySet = TORRENT_BASELINE.overridden(
        fields={
            Field.ETA_S: Capability(
                Support.DERIVED,
                note=(
                    "doc-derived, UNVERIFIED 2026-08-22: computed as d.left_bytes/d.down.rate; "
                    "None whenever the current rate is zero, and swings with the instantaneous "
                    "rate rather than being a value rTorrent itself reports"
                ),
            ),
            Field.SEED_TIME_S: Capability(
                Support.DERIVED,
                note=(
                    "wall-clock since d.timestamp.finished, not time actually spent seeding -- "
                    "a stopped torrent still accrues (spec §4.3's own canonical example). "
                    "None while incomplete."
                ),
            ),
        },
    )

    config_schema = (
        ConfigField(
            key="base_url",
            label="Base URL",
            kind="str",
            help_text="e.g. http://seedbox:8080 -- the host rTorrent's XML-RPC endpoint is on.",
        ),
        ConfigField(
            key="rpc_path",
            label="RPC path",
            kind="str",
            required=False,
            default=DEFAULT_RPC_PATH,
            help_text=(
                "Direct XML-RPC is the default (MEASURED 2026-08-22: /RPC2 behind HTTP Basic "
                "auth). Change this if your deployment only exposes a ruTorrent plugin mount, "
                "e.g. /rutorrent/plugins/httprpc/action.php."
            ),
        ),
        ConfigField(
            key="username",
            label="Username",
            kind="str",
            required=False,
            help_text="HTTP Basic auth username, if the endpoint requires one.",
        ),
        ConfigField(
            key="password",
            label="Password",
            kind="secret",
            required=False,
            help_text="HTTP Basic auth password, if the endpoint requires one.",
        ),
    )

    def __init__(self, *, config: dict[str, Any]) -> None:
        super().__init__(config=config)
        base_url = str(config["base_url"]).rstrip("/")
        self._rpc_path = str(config.get("rpc_path") or DEFAULT_RPC_PATH)
        if not self._rpc_path.startswith("/"):
            self._rpc_path = f"/{self._rpc_path}"
        username = config.get("username")
        password = config.get("password")
        self._password = str(password) if password else ""
        auth = (
            httpx.BasicAuth(str(username or ""), self._password) if username or password else None
        )
        self._client = httpx.AsyncClient(base_url=base_url, timeout=DEFAULT_TIMEOUT_S, auth=auth)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> RtorrentClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    @staticmethod
    def map_phase(raw_status: str) -> TransferPhase:
        return _map_phase(raw_status)

    # ------------------------------------------------------------------------------------
    # Transport -- the one place the three-way error taxonomy (spec §4.2) is drawn, and the one
    # place an XML-RPC fault is classified (spec §4.2's `ClientError`-vs-`CapabilityUnavailable`
    # split, judged per-fault-text rather than per-method -- see module-level markers above).
    # ------------------------------------------------------------------------------------

    async def _call(self, method: str, *params: Any, capture: bool = False) -> Any:
        """One `POST <rpc_path>` XML-RPC round trip, built and parsed with the stdlib marshaller
        (`xmlrpc.client.dumps`/`loads`) over the existing async `httpx` client -- never
        `xmlrpc.client.ServerProxy` (synchronous, would block the event loop).

        Raises `ClientUnreachable` for a transport-level failure, `ClientAuthenticationFailed`
        for the **MEASURED** 401 (module docstring), `CapabilityUnavailable` when a returned
        XML-RPC fault's text looks like "this command does not exist here"
        (`_looks_like_missing_method`, doc-derived, UNVERIFIED), and `ClientError` for every
        other fault, a non-2xx status this method cannot otherwise classify, or a response body
        that does not parse as XML-RPC at all.
        """
        body = xmlrpc.client.dumps(params, methodname=method)
        try:
            response = await self._client.post(
                self._rpc_path, content=body.encode("utf-8"), headers={"Content-Type": "text/xml"}
            )
        except httpx.TransportError as exc:
            raise ClientUnreachable(f"rtorrent {method} unreachable: {exc}") from exc
        except httpx.HTTPError as exc:
            raise ClientError(f"rtorrent {method} failed: {exc}") from exc

        if capture:
            # See module docstring: unlike SABnzbd's `apikey` query parameter, HTTP Basic auth
            # does not put the credential in the URL, so httpx's own default request-URL logging
            # (spec §13.3's "side door") does not reapply here. The password is still redacted
            # from the captured sample as defense in depth -- it is never expected to appear in
            # an XML-RPC body, but "expected not to" is exactly the standard spec §13.3 already
            # rejected once for this same reason.
            raw_sample = f"POST {response.request.url}\n{body}\n---\n{response.text}"
            logger.debug(
                "rtorrent %s response: %s",
                method,
                capture_response(raw_sample, secrets=(self._password,) if self._password else ()),
            )

        # MEASURED, 2026-08-22 (module docstring): checked before any attempt to parse a body --
        # a 401 from the reverse proxy/rTorrent's own auth layer carries no XML-RPC payload at
        # all on the live instance's probed endpoints.
        if response.status_code == 401:
            raise ClientAuthenticationFailed(
                f"rtorrent {method} rejected the configured credentials"
            )

        try:
            response_params, _dispatch_name = xmlrpc.client.loads(response.text)
            result = response_params[0]
        except xmlrpc.client.Fault as exc:
            if _looks_like_missing_method(exc.faultString):
                raise CapabilityUnavailable(
                    f"rtorrent does not support {method}: {exc.faultString}"
                ) from exc
            raise ClientError(f"rtorrent {method} returned a fault: {exc.faultString}") from exc
        except Exception as exc:  # noqa: BLE001 - anything else means "not parseable as XML-RPC"
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as http_exc:
                raise ClientError(
                    f"rtorrent {method} returned HTTP {response.status_code}"
                ) from http_exc
            raise ClientError(f"rtorrent {method} returned an unparseable response") from exc
        return result

    # ------------------------------------------------------------------------------------
    # Normalization -- one `d.multicall2` row -> `Transfer`.
    # ------------------------------------------------------------------------------------

    def _transfer_from_row(self, row: dict[str, Any]) -> Transfer:
        client_id = normalize_client_id(str(row.get("hash") or ""))

        hashing = _flag(row.get("hashing"))
        complete = _flag(row.get("complete"))
        is_active = _flag(row.get("is_active"))
        # `d.state=` -- doc-derived, UNVERIFIED: 1 means the download has been `d.start`ed
        # (registered as "open"), 0 means it has never been started or was explicitly
        # `d.stop`ped. Used only as the PAUSED/QUEUED tiebreaker (module-level comment above).
        state_started = _flag(row.get("state"))
        raw_status = _classify_token(
            hashing=hashing, complete=complete, is_active=is_active, state_started=state_started
        )

        size_bytes = _int_or_none(row.get("size_bytes"))
        completed_bytes = _int_or_none(row.get("completed_bytes"))
        left_bytes = _int_or_none(row.get("left_bytes"))
        down_rate = _int_or_none(row.get("down_rate"))

        eta_s: int | None = None
        if left_bytes is not None and down_rate:
            eta_s = left_bytes // down_rate

        ratio_raw = _int_or_none(row.get("ratio"))
        # **Per-mille -- divide by 1000** (spec §2.2, §5, the survey's own headline trap). A rule
        # comparing raw `d.ratio` against `1.0` would treat every torrent as wildly over-seeded.
        # The divisor lives in exactly this one place, on purpose.
        ratio = (ratio_raw / 1000.0) if ratio_raw is not None else None

        timestamp_finished = _int_or_none(row.get("timestamp_finished"))
        # **`SEED_TIME_S` is `Support.DERIVED`, not `NATIVE`** (spec §4.3's own canonical
        # example, TORRENT_BASELINE's own comment): rTorrent has no seed-time field at all.
        # Deriving it as wall-clock-since-completion means a *stopped* torrent still accrues --
        # not the same thing a "seeded for 14 days" rule means. Only computed while `complete`,
        # since a still-downloading item has no completion instant to measure from.
        seed_time_s: int | None = None
        if complete and timestamp_finished:
            seed_time_s = max(int(datetime.now(tz=UTC).timestamp()) - timestamp_finished, 0)

        message = str(row.get("message") or "") or None
        category = str(row.get("custom1") or "") or None

        transfer = Transfer(
            client_id=client_id,
            name=str(row.get("name") or ""),
            phase=self.map_phase(raw_status),
            raw_status=raw_status,
            raw=row,
            content_path=str(row.get("base_path") or "") or None,
            size_bytes=size_bytes,
            bytes_done=completed_bytes,
            eta_s=eta_s,
            error_message=message,
            category=category,
            added_at=_epoch_to_iso(row.get("timestamp_started")),
            completed_at=_epoch_to_iso(timestamp_finished),
            ratio=ratio,
            uploaded_bytes=_int_or_none(row.get("up_total")),
            seed_time_s=seed_time_s,
        )
        return project_transfer(transfer, self.capabilities)

    # ------------------------------------------------------------------------------------
    # `DownloadClient` interface.
    # ------------------------------------------------------------------------------------

    async def test_connection(self) -> ConnectionInfo:
        """A single authenticated call -- unlike SABnzbd, rTorrent's Basic auth applies
        uniformly to every endpoint on the probed deployment (module docstring), so there is no
        separate unauthenticated call needed just to prove reachability. `system.client_version`
        is doc-derived, UNVERIFIED: chosen as the smallest, side-effect-free call that both
        proves the credentials work and returns something worth showing as a version string.
        """
        version = await self._call("system.client_version", capture=True)
        version_str = str(version) if version is not None else None
        return ConnectionInfo(version=version_str, raw={"client_version": version})

    async def _list_all(self) -> list[Transfer]:
        commands = [cmd for _key, cmd in _LISTING_FIELDS]
        # `("", "main", *commands)` -- the leading empty string is a doc-derived, UNVERIFIED
        # "call id" convention several rTorrent client libraries pass to `d.multicall2` (ignored
        # by rTorrent itself on every reading this module found, but included since omitting a
        # required-by-convention argument is a more plausible failure than a harmlessly-unused
        # extra one).
        rows = await self._call("d.multicall2", "", "main", *commands)
        if not isinstance(rows, (list, tuple)):
            return []
        return [self._transfer_from_row(_row_to_dict(row)) for row in rows]

    async def list_transfers(self, *, active_only: bool = False) -> list[Transfer]:
        """`d.multicall2` against the `main` view, in one round trip, requesting every field a
        declared `Field` needs (spec's own explicit instruction: never per-torrent calls here --
        that is what `list_trackers`/`list_files` are separately for).

        `active_only` -- doc-derived, UNVERIFIED, and a pure client-side filter rather than a
        second query against a different rTorrent view: rTorrent has only one list (spec §5's
        `LIST_HISTORY: DERIVED, "a torrent never leaves the list"`), so there is nothing cheaper
        to ask for. `COMPLETED` (complete + stopped) is the only phase excluded -- a torrent a
        user has finished and stopped caring about is exactly what the fast cadence
        (spec §9.1: settle-gate skip, Preflight, withhold) does not need to see every ~10s.
        """
        transfers = await self._list_all()
        if not active_only:
            return transfers
        return [t for t in transfers if t.phase is not TransferPhase.COMPLETED]

    async def list_history(self) -> list[Transfer]:
        """Derived (`TORRENT_BASELINE`: "a torrent never leaves the list") -- filters the full
        listing to `SEEDING`/`COMPLETED`, the two phases whose content is fully downloaded and
        whose `content_path` is therefore meaningful the way spec §2.1's `list_history` contract
        ("carrying the real on-disk path") expects.
        """
        transfers = await self._list_all()
        return [t for t in transfers if t.phase in (TransferPhase.SEEDING, TransferPhase.COMPLETED)]

    async def get_transfer(self, client_id: str) -> Transfer | None:
        wanted = normalize_client_id(client_id)
        for transfer in await self._list_all():
            if transfer.client_id == wanted:
                return transfer
        return None

    async def list_trackers(self, client_id: str) -> list[TrackerInfo]:
        """`t.multicall` -- doc-derived, UNVERIFIED. **Hostname only, never the full announce
        URL** (spec §7.3): a full announce URL embeds a per-user passkey, and there is no field
        on `TrackerInfo` to hold one even by mistake -- the host is extracted here, at the point
        of construction, not left to a caller to remember to redact later.
        """
        rtorrent_hash = _to_rtorrent_hash(client_id)
        rows = await self._call("t.multicall", rtorrent_hash, "", "t.url=")
        hosts: list[str] = []
        for row in rows if isinstance(rows, (list, tuple)) else []:
            url = row[0] if isinstance(row, (list, tuple)) and row else None
            if not isinstance(url, str) or not url:
                continue
            netloc = urlsplit(url).netloc
            if netloc:
                hosts.append(netloc)
        return [TrackerInfo(host=host) for host in hosts]

    async def list_files(self, client_id: str) -> list[str]:
        """`f.multicall2` -- doc-derived, UNVERIFIED, mirroring the listing multicall's own
        naming. Tolerant of an unexpected row shape, same "skip rather than raise" discipline
        `sabnzbd.py.list_files` uses for its own doc-derived response shape.
        """
        rtorrent_hash = _to_rtorrent_hash(client_id)
        rows = await self._call("f.multicall2", rtorrent_hash, "", "f.path=")
        files: list[str] = []
        for row in rows if isinstance(rows, (list, tuple)) else []:
            path = row[0] if isinstance(row, (list, tuple)) and row else None
            if isinstance(path, str) and path:
                files.append(path)
        return files

    async def list_base_paths(self) -> list[BasePath]:
        """`directory.default` -- MEASURED-adjacent choice (prompt's explicit instruction),
        reported as `BasePathKind.WORKING`. Per spec §1.1, rTorrent will *never* report the
        completed folder it hardlinks into, and that is expected and correct: that folder is a
        queue's `remote_path`, already known to lftpweb on its own (spec §8.2's correction) --
        not something any connector needs to supply.
        """
        value = await self._call("directory.default")
        path = str(value or "").strip()
        if not path:
            return []
        return [BasePath(path=path, kind=BasePathKind.WORKING)]

    async def free_space(self, path: str) -> SpaceInfo:
        """No generic "free space at an arbitrary path" call exists in rTorrent's own vocabulary
        (doc-derived, UNVERIFIED) -- `d.free_diskspace` is answered *per download* (and is
        itself the *minimum* across the devices that download's files span, spec §12's own
        trap), not per filesystem path. Best-effort implementation: find a currently-listed
        transfer whose `content_path` sits at or under `path`, and read its `d.free_diskspace`.
        Raises `ClientError` (not `CapabilityUnavailable` -- the capability isn't in question,
        there is simply no torrent to answer through right now) when nothing matches.
        """
        transfers = await self._list_all()
        match = next(
            (
                t
                for t in transfers
                if t.content_path
                and (t.content_path == path or t.content_path.startswith(path.rstrip("/") + "/"))
            ),
            None,
        )
        if match is None:
            raise ClientError(f"no rtorrent item found under {path!r} to read free space from")
        rtorrent_hash = _to_rtorrent_hash(match.client_id)
        free_bytes = await self._call("d.free_diskspace", rtorrent_hash)
        parsed = _int_or_none(free_bytes)
        if parsed is None:
            raise ClientError(
                f"rtorrent d.free_diskspace returned a non-numeric value: {free_bytes!r}"
            )
        return SpaceInfo(free_bytes=parsed, total_bytes=None)

    async def pause(self, client_id: str) -> None:
        """`d.pause` -- doc-derived, UNVERIFIED, and deliberately **not** `d.stop`: rTorrent
        distinguishes a lightweight pause (`d.pause`/`d.resume`, keeps the download "open") from
        a full stop (`d.stop`/`d.start`, closes tracker/peer connections) -- `remove()` uses the
        heavier `d.stop` on purpose, per this task's own explicit instruction, immediately before
        `d.erase`; this operation is the ordinary "pause" a user or a rule invokes and is not
        expected to precede a removal.
        """
        rtorrent_hash = _to_rtorrent_hash(client_id)
        await self._call("d.pause", rtorrent_hash)

    async def resume(self, client_id: str) -> None:
        rtorrent_hash = _to_rtorrent_hash(client_id)
        await self._call("d.resume", rtorrent_hash)

    async def remove(self, client_id: str) -> RemoveOutcome:
        """Unregister the item, **leave the data on disk** -- `d.stop` then `d.erase`, in that
        order, and **nothing else** (spec §10.1, §2.1; this task's own hard rule). There is no
        `d.custom5.set`, no `d.delete_tied`, and no code path in this module that could construct
        either call -- `docs/download-client-api-survey.md` §2's `erasedata` hook sequence is not
        merely unused here, it does not exist in this file at all.

        A fault whose text looks like "no such hash" (`_looks_like_unknown_hash`, doc-derived,
        UNVERIFIED) reads as a routine "already gone" outcome -- `RemoveOutcome(succeeded=False)`
        -- rather than a raised error, the same tolerant reading `sabnzbd.py.remove`'s
        queue-then-history fallback gives an id found in neither place. Any other `ClientError`
        (a genuine transport/protocol failure) is **not** swallowed here and propagates normally,
        unlike SABnzbd's fallback, which has no equivalent "this call failed for a real reason"
        case to distinguish since its own failure signal rides in response *data*, not a raised
        exception.
        """
        rtorrent_hash = _to_rtorrent_hash(client_id)
        try:
            await self._call("d.stop", rtorrent_hash)
            await self._call("d.erase", rtorrent_hash)
        except ClientError as exc:
            if _looks_like_unknown_hash(str(exc)):
                return RemoveOutcome(succeeded=False, detail=str(exc))
            raise
        return RemoveOutcome(succeeded=True, detail="stopped and erased")

    async def set_label(self, client_id: str, label: str) -> None:
        """`d.custom1.set` -- doc-derived, UNVERIFIED, and the write-side counterpart of the
        `Field.CATEGORY` guess this module trusts least (see `_LISTING_FIELDS`'s own comment,
        spec §13.6). Writing to a generic user slot cannot itself corrupt anything rTorrent
        depends on, which is why this is implemented despite the read-side uncertainty -- worst
        case, the label silently does not round-trip through whatever UI (if any) a given
        deployment's ruTorrent install reads it from.
        """
        rtorrent_hash = _to_rtorrent_hash(client_id)
        await self._call("d.custom1.set", rtorrent_hash, label)

    async def recheck(self, client_id: str) -> None:
        """`d.check_hash` -- doc-derived, UNVERIFIED. Torrent-only, native per `TORRENT_BASELINE`."""
        rtorrent_hash = _to_rtorrent_hash(client_id)
        await self._call("d.check_hash", rtorrent_hash)
