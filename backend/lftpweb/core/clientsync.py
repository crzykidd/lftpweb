"""The download-client poller (docs/download-client-framework-spec.md §9, stage 2a of #18) --
background loop, `_task`/`start()`/`stop()`/`is_alive`, same shape `core/arrsync.py.
ArrSyncScheduler` and `core/backup.py.BackupScheduler` already use for "one bad cycle must not
kill the loop." **Build alongside `core/arrsync.py`, never inside it** (spec §9's own explicit
instruction, this task's own handoff prompt) -- the only touch this task makes to that file is
one additive field on an existing `PreflightRow(...)` call (see that module's own comment at the
call site) so this poller's §9.2 merge can dedupe by `downloadId` instead of a title heuristic;
nothing about `arrsync.py`'s own control flow changes.

**The rule this module must never break** (spec §1, §4.1, this task's own handoff prompt): a
connector is handed no database handle and cannot write `item.state` because it has nothing to
write to. This poller *is* handed one (`self.db`, for reading instance config and category
mappings, and for `audit.record_event`) -- the structural guarantee that protects the connector
layer does **not** protect this module by construction, only by discipline. **This module never
writes `item.state`, directly or through a helper, for any reason.** It caches what a client
reports and projects it into the Preflight box (spec §9.2) -- exactly the same "observes, never
decides" boundary `core/preflight.py`'s existing sources already respect.

**Two cadences per instance (spec §9.1)**, driven by one loop tick (`FAST_INTERVAL_S`), not two
separate tasks or two separate calls: every tick, `list_transfers(active_only=True)` feeds the
Preflight box; at most once every `SLOW_INTERVAL_S`, that call widens to `active_only=False` (the
full estate) instead, and the result is cached for a future consumer (#21's seeding overview --
nothing reads `_full_estate` yet in stage 2a, but the cache exists so that work doesn't also have
to build the poll). **One call per tick, never two** -- the full-estate result is a strict
superset of the active-only one (every connector's own contract), so a tick that's due for the
slow cadence simply asks the wider question instead of asking both; `_update_preflight` already
filters to non-terminal transfers regardless of which shape it was handed. Both cadences share
one instance-level backoff: a failure on either question means the identical thing (this
instance cannot currently be reached/authenticated), so there is no value in tracking them as two
independent health signals.

**No connector-level timeout is set here.** Every real connector already opens its own
`httpx.AsyncClient` with a 10s timeout at construction (`SabnzbdClient`/`RtorrentClient`'s own
`DEFAULT_TIMEOUT_S`) -- spec §9's "10s timeout" requirement is already satisfied at the layer
that owns the transport; reaching into a connector to override it here would be exactly the kind
of per-client special-casing spec §5.1 forbids.

**`ClientAuthenticationFailed` gets the same backoff ladder as every other failure, but its own
audit `kind`.** A wrong credential will not fix itself by retrying (spec's own framing), but the
credential lives in the database and can change out from under this poller at any time (a user
fixing it in Settings), so the ladder still exists to let the very next pass after backoff
recover automatically -- there is no reason to abandon this instance permanently just because
today's failure mode isn't transient. What *does* need to be different is the message: an
operator seeing "unreachable" checks the network; an operator seeing "authentication failed"
re-enters an API key. `_failure_kind` draws that line, and `_handle_failure` only re-reports when
either a brand-new failure streak starts *or* the kind of an ongoing one changes (spec: "one
event per failure transition, not per failed pass") -- a `ClientUnreachable` streak that later
starts answering with `ClientAuthenticationFailed` is a materially different fact worth a fresh
event, even mid-outage.

**No `PreflightHold` retirement set, unlike `core/arrsync.py`'s `_preflight_candidates`.** The
*arr source actively retires a row the instant a poll pass proves it has become a real `item`
(evicting immediately rather than waiting out `PREFLIGHT_HOLD_S`) because it can cheaply check
"does a matching item exist" via `item.rel_path`. This source has no equivalent cheap check --
attribution here is category -> queue (spec §8.3), not a name/path match against `item`, so
"has this transfer become a real item yet" isn't a question this module can answer without
re-deriving the *arr's own matching logic a second time. The un-refined fallback is exactly the
one `core/arrsync.py`'s own docstring already blesses for a structurally identical case
(`TRACKED_DOWNLOAD_STATE_IMPORTED` exclusion: "deliberately not added to retired... falls out
through the ordinary hold-then-expire path"): once a transfer completes or is removed at the
client, it simply stops appearing in `active_only=True`'s result, ages out of the hold after
`PREFLIGHT_HOLD_S` (150s) with no special-casing, and is gone. A few minutes of possible overlap
with a freshly-created `item` is the accepted cost, not a defect -- the same cost arrsync's
own eviction-latency fix (2026-08-21) existed to *shrink*, not eliminate, for its own source.

**`PreflightHold` *is* used here**, deliberately -- this task's own handoff prompt asks the
question directly, and the answer is yes: a download client's own queue/list endpoint blanking
out for one poll is not a hypothetical for this source, it is *literally* the v0.2.4 SABnzbd
production incident (spec §1, §4.2) reached by the most direct route available -- this module
polls the same SABnzbd the *arr does, over the same kind of flaky queue endpoint. Without the
hold, a blank response would wipe every active row from this source's Preflight rows for a
single pass, then have them reappear -- the exact flicker spec §4.2 exists to prevent.
`tests/fake_sabnzbd.py`'s `queue_empty_for_requests` (this fixture's own blank-queue mode, built
for precisely this incident) is what this module's own tests drive to prove it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass

import aiosqlite

from lftpweb.core import audit
from lftpweb.core.clients import get_client_class
from lftpweb.core.clients.errors import ClientAuthenticationFailed, ClientError, ClientUnreachable
from lftpweb.core.clients.models import Transfer, TransferPhase
from lftpweb.core.crypto import DecryptionError, decrypt_secret
from lftpweb.core.preflight import PreflightHold, PreflightRow

logger = logging.getLogger(__name__)

# --- Two cadences (spec §9.1) ------------------------------------------------------------------

# Matches `core/arrsync.py.ArrSettings`'s own current default (10s, 2026-08-21's issue #16) --
# this is the cadence the settle-gate skip (#18's own stage 2b, out of scope here) and Preflight
# both need, per spec §9.1's own table. Not settings-driven (unlike the *arr's own cadence) --
# this task's scope is the poller and its cache, not a Settings UI knob nobody has asked for yet;
# a fixed constant is the honest reflection of that until real use says otherwise.
FAST_INTERVAL_S = 10.0

# "Minutes," per spec §9.1's own table -- 5 minutes is a deliberate, round, un-agonized-over
# choice: slow enough that listing hundreds of seeding torrents doesn't happen every tick (spec's
# own "waste" framing), fast enough that #21's future seeding-overview consumer isn't working
# from a stale-by-an-hour picture. Not a settings knob, for the same reason `FAST_INTERVAL_S`
# isn't one.
SLOW_INTERVAL_S = 300.0

# --- Per-instance failure isolation (spec §9, mirrors `core/arrsync.py`'s own constants exactly
# -- "60s -> 30min", the shape this task's own handoff prompt names as the one to reuse) --------

INITIAL_BACKOFF_S = 60.0
MAX_BACKOFF_S = 1800.0  # 30 minutes
BACKOFF_FACTOR = 2.0


@dataclass
class _InstanceBackoff:
    delay_s: float
    next_attempt_at: float  # `run_once`'s own `now` clock (time.monotonic() in production)
    # The audit `kind` last reported for this failure streak -- `_handle_failure`'s own "one
    # event per transition" rule reports again only when this changes (a fresh streak, or the
    # same streak's failure mode itself changing, e.g. unreachable -> auth failure), never on
    # every backed-off retry attempt.
    last_kind: str


def _failure_kind(exc: Exception) -> str:
    """The audit `kind` a given failure reports as -- spec §4.2's three-way taxonomy, plus the
    two failure shapes that never reach a connector at all (a secret this process can no longer
    decrypt, an unregistered `client_type`), both of which mean the same thing to an operator as
    "cannot currently be reached": there is nothing to retry differently, only something to fix
    in Settings.
    """
    if isinstance(exc, ClientAuthenticationFailed):
        return "client_auth_failed"
    if isinstance(exc, ClientUnreachable | DecryptionError):
        return "client_unreachable"
    if isinstance(exc, KeyError):
        return "client_unknown_type"
    return "client_error"  # the base ClientError, or CapabilityUnavailable from a mandatory op


_FAILURE_VERB: dict[str, str] = {
    "client_auth_failed": "rejected the configured credential",
    "client_unreachable": "unreachable",
    "client_unknown_type": "has an unregistered client_type",
    "client_error": "returned an error",
}


class ClientSyncScheduler:
    """Background loop, one pass over every **enabled** `download_client` instance per tick
    (spec §9: "A disabled instance is never contacted"). See this module's own docstring for the
    two-cadence shape, the backoff/event rules, and why `PreflightHold` is used here.

    `config_dir` is needed to decrypt each instance's `secret_enc` fresh on every poll pass, the
    same "never outlive the pass that used it" reasoning `core/arrsync.py.ArrSyncScheduler`
    already documents for the *arr's own API key.
    """

    FAST_INTERVAL_S = FAST_INTERVAL_S
    SLOW_INTERVAL_S = SLOW_INTERVAL_S

    def __init__(self, db: aiosqlite.Connection, config_dir: str) -> None:
        self.db = db
        self.config_dir = config_dir
        self._task: asyncio.Task | None = None
        self._backoff: dict[int, _InstanceBackoff] = {}
        self._last_slow_poll_at: dict[int, float] = {}
        # The slow cadence's own cache -- nothing in stage 2a reads this (#21's own future job);
        # it exists so that work starts from an already-running poll rather than building one.
        # Keyed by instance id -> client_id -> Transfer, wholesale-replaced each slow pass.
        self._full_estate: dict[int, dict[str, Transfer]] = {}
        # The Preflight source (spec §9.2) -- one `PreflightHold` per instance id, the identical
        # flap-tolerant shape `core/arrsync.py.ArrSyncScheduler._preflight_holds` uses, for the
        # identical reason (this module's own docstring).
        self._preflight_holds: dict[int, PreflightHold] = {}

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="lftpweb-client-sync-loop")

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
                logger.exception("client sync cycle failed")
            await asyncio.sleep(self.FAST_INTERVAL_S)

    # --- One pass over every enabled instance ------------------------------------------------

    async def run_once(self, *, now: float | None = None) -> None:
        """`now` is overridable so a test can drive the two cadences and the backoff ladder
        without sleeping real wall-clock seconds -- the same `now`-override shape
        `core/arrsync.py._dropped_grace_expired` already uses for identical reasons.
        """
        now = now if now is not None else time.monotonic()
        cursor = await self.db.execute(
            "SELECT id, name, client_type, config_json, secret_enc FROM download_client "
            "WHERE enabled = 1"
        )
        instances = await cursor.fetchall()
        for instance in instances:
            await self._process_instance(instance, now)

    async def _process_instance(self, instance: aiosqlite.Row, now: float) -> None:
        instance_id = instance["id"]
        instance_name = instance["name"]

        backoff = self._backoff.get(instance_id)
        if backoff is not None and now < backoff.next_attempt_at:
            return  # still backing off -- never blocks another instance (spec)

        try:
            client_class = get_client_class(instance["client_type"])
        except KeyError as exc:
            await self._handle_failure(instance_id, instance_name, exc, now)
            return

        secret: dict = {}
        if instance["secret_enc"]:
            try:
                secret = json.loads(decrypt_secret(self.config_dir, instance["secret_enc"]))
            except DecryptionError as exc:
                await self._handle_failure(instance_id, instance_name, exc, now)
                return
        non_secret = json.loads(instance["config_json"]) if instance["config_json"] else {}
        client = client_class(config={**non_secret, **secret})

        try:
            # The slow cadence's own `active_only=False` result is a strict superset of the fast
            # cadence's `active_only=True` one (every connector's own contract, spec §9.1's
            # table) -- so on a tick where the slow poll is due, there is no reason to also pay
            # for a separate fast-only call; `_update_preflight` already filters out terminal
            # transfers defensively, so the full-estate result feeds the Preflight projection
            # exactly as a fast-only result would have. Only on a tick where the slow poll is
            # *not* due does this actually make the cheaper `active_only=True` call.
            last_slow = self._last_slow_poll_at.get(instance_id, 0.0)
            slow_due = now - last_slow >= self.SLOW_INTERVAL_S
            try:
                transfers = await client.list_transfers(active_only=not slow_due)
            except ClientError as exc:
                await self._handle_failure(instance_id, instance_name, exc, now)
                return

            self._backoff.pop(instance_id, None)  # reachable (and authenticated) again
            await self._update_preflight(instance, transfers, now)
            if slow_due:
                self._full_estate[instance_id] = {t.client_id: t for t in transfers}
                self._last_slow_poll_at[instance_id] = now
        finally:
            aclose = getattr(client, "aclose", None)
            if aclose is not None:
                await aclose()

    async def _handle_failure(
        self, instance_id: int, instance_name: str, exc: Exception, now: float
    ) -> None:
        kind = _failure_kind(exc)
        prior = self._backoff.get(instance_id)
        delay = (
            INITIAL_BACKOFF_S
            if prior is None
            else min(prior.delay_s * BACKOFF_FACTOR, MAX_BACKOFF_S)
        )
        should_report = prior is None or prior.last_kind != kind
        self._backoff[instance_id] = _InstanceBackoff(
            delay_s=delay, next_attempt_at=now + delay, last_kind=kind
        )
        if not should_report:
            return  # same ongoing outage, already reported once (spec: "not per failed pass")
        logger.warning(
            "download client %d (%s) %s, backing off %.0fs: %s",
            instance_id,
            instance_name,
            _FAILURE_VERB[kind],
            delay,
            exc,
        )
        await audit.record_event(
            self.db,
            level="warning",
            kind=kind,
            message=(
                f"download client {instance_name!r} (id={instance_id}) "
                f"{_FAILURE_VERB[kind]}: {exc}; backing off {delay:.0f}s"
            ),
        )

    # --- Preflight (spec §9.2) ----------------------------------------------------------------

    async def _category_queue_map(self, instance_id: int) -> dict[str, aiosqlite.Row]:
        """This instance's own category -> queue binding (spec §8.3), restricted to currently
        **enabled** queues -- a category mapped to a disabled queue is exactly as unattributable
        as one mapped to nothing at all. Fresh every pass, the same "no cache staleness on a
        config change" reasoning every other live query in this codebase's poller-adjacent code
        already follows.
        """
        cursor = await self.db.execute(
            "SELECT download_client_category.category AS category, path_queue.id AS id, "
            "path_queue.name AS name, path_queue.short_name AS short_name "
            "FROM download_client_category JOIN path_queue "
            "ON path_queue.id = download_client_category.queue_id "
            "WHERE download_client_category.client_id = ? AND path_queue.enabled = 1",
            (instance_id,),
        )
        rows = await cursor.fetchall()
        return {r["category"]: r for r in rows}

    async def _update_preflight(
        self, instance: aiosqlite.Row, transfers: list[Transfer], now: float
    ) -> None:
        """Project this pass's active transfers into Preflight rows (spec §9.2, "The Preflight
        source") and refresh this instance's own `PreflightHold`. Attribution is category ->
        queue (spec §8.3); a transfer whose category doesn't map to a currently-enabled queue is
        **silently omitted** -- `core/preflight.py`'s own established rule, restated by this
        task's own handoff prompt: "promising a release that never arrives is worse than showing
        nothing."
        """
        instance_id = instance["id"]
        category_map = await self._category_queue_map(instance_id)

        seen: dict[str, PreflightRow] = {}
        for transfer in transfers:
            # Defensive only -- every connector's own `active_only=True` contract already
            # excludes terminal transfers (spec §9.1's own table); this is a second guard
            # against a connector that doesn't honor the flag perfectly, never load-bearing.
            if transfer.phase in (TransferPhase.COMPLETED, TransferPhase.FAILED):
                continue
            if not transfer.category:
                continue  # unattributable -- no category reported at all
            queue = category_map.get(transfer.category)
            if queue is None:
                continue  # unattributable -- category not mapped to an enabled queue

            size_remaining_bytes = None
            if transfer.size_bytes is not None and transfer.bytes_done is not None:
                size_remaining_bytes = max(transfer.size_bytes - transfer.bytes_done, 0)
            # `eta_s == 0` is treated as "no meaningful estimate," the identical instinct
            # `core/arrsync.py._parse_timeleft` applies to a parsed `00:00:00` -- a paused or
            # stalled transfer reporting exactly zero seconds left is not a real ETA (the
            # handoff prompt's own "never a fabricated or zero figure" instruction).
            remaining_s = float(transfer.eta_s) if transfer.eta_s else None

            seen[transfer.client_id] = PreflightRow(
                source="client",
                queue_id=queue["id"],
                queue_name=queue["name"],
                queue_short_name=queue["short_name"],
                title=transfer.name,
                status_label=transfer.raw_status,
                source_label=instance["name"],
                source_kind=instance["client_type"],
                size_bytes=transfer.size_bytes,
                size_remaining_bytes=size_remaining_bytes,
                remaining_s=remaining_s,
                # This source *is* the download client -- there is no separate "which client is
                # fetching this" to report, the same reason a settle-gated row leaves this None
                # (`core/preflight.py.PreflightRow`'s own docstring).
                download_client=None,
                wait_scans=None,
                wait_since=None,
                # spec §9.2's merge key -- already normalized by the connector itself
                # (`core.clients.models.normalize_client_id`, applied inside every connector's
                # own `_transfer_from_*` construction) before it ever reaches this module.
                download_id=transfer.client_id,
            )

        hold = self._preflight_holds.setdefault(instance_id, PreflightHold())
        # No `retired` set -- see this module's own docstring for why this source falls back to
        # the plain hold-then-expire path rather than `core/arrsync.py`'s active eviction.
        hold.update(seen, now=now)

    def preflight_rows(self, enabled_instance_ids: frozenset[int]) -> list[PreflightRow]:
        """The Preflight box's own read (`api/jobs.py.get_preflight`) -- every currently-held row
        from an instance id in `enabled_instance_ids`, that caller's own live "is this instance
        still enabled, with at least one enabled bound queue" check. Sorted alphabetically by
        title, case-insensitively -- the same boring-default rule every other Preflight source
        already applies (`core/arrsync.py.ArrSyncScheduler.preflight_rows`, `core/autoqueue.py.
        AutoQueue.preflight_rows`).

        Synchronous, unlike `ArrSyncScheduler.preflight_rows` -- this source has no request-time
        retirement re-check to perform (this module's own docstring), so there is no `await` this
        method would ever need to make.
        """
        rows: list[PreflightRow] = []
        for instance_id, hold in self._preflight_holds.items():
            if instance_id not in enabled_instance_ids:
                continue
            rows.extend(hold.rows())
        rows.sort(key=lambda r: r.title.casefold())
        return rows
