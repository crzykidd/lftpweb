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

**Two cadences per instance (spec §9.1), split along *cheap-vs-expensive*, not *active-vs-
everything*.** The original stage 2a build drew the split on `active_only` alone: fast tick asks
`list_transfers(active_only=True)`, slow tick (`SLOW_INTERVAL_S`) widens to the full estate. Stage
2b then discovered that split **structurally cannot carry a terminal verdict**: a finished
SABnzbd item leaves the queue entirely and appears only in history, and `active_only=True` never
sees it -- both connectors' own contract excludes every terminal transfer from that call. So a
"completed" or "failed" fact was always stranded behind `SLOW_INTERVAL_S` (5 minutes), no matter
how often the fast tick ran, largely defeating spec §4.3's whole point (skip/withhold exist to
replace a wait with a direct, *prompt* observation).

**The fix (this stage): the split is a per-connector fact, read off the same `CapabilitySet`
every other layer of this framework already consults -- never `if client_type == ...`.**
`Operation.LIST_HISTORY`'s own NATIVE/DERIVED declaration (spec §5) already says exactly which
half a connector falls into:

- **NATIVE** (SABnzbd, `USENET_BASELINE`) means a real, independent, trivial call --
  `mode=history` costs nothing extra to make every fast tick, and it is where every terminal
  verdict lives. So a non-slow tick calls `list_transfers(active_only=True)` **and**
  `list_history()`, and the terminal (`COMPLETED`/`FAILED`) results of the latter are merged
  straight into `_full_estate` -- the cache `completed_transfers()`/`failed_transfers()` read --
  without waiting for the next slow pass.
- **DERIVED** (rTorrent, `TORRENT_BASELINE`: "a torrent never leaves the list") means
  `list_history()` is not a second cheap call at all -- it re-fetches the *same* expensive full
  listing `list_transfers(active_only=False)` already pays for (`RtorrentClient.list_transfers`/
  `list_history` both call `_list_all()`). Calling it every fast tick would double the exact cost
  spec §9.1 exists to avoid ("listing 500 seeding torrents every 10 seconds is waste"), so it is
  left to the slow cadence, unchanged from stage 2a.

`capabilities.supports(Operation.LIST_HISTORY)` (native only, `accept_derived` left `False`) is
the one query this module makes to decide -- the entire per-connector distinction, with no
connector-specific branch anywhere in this scheduler. The slow cadence's own full-estate refresh
(`active_only=False`, at most once every `SLOW_INTERVAL_S`) is unchanged: still the only call for
a genuinely expensive listing, still cached wholesale for #21's future seeding-overview consumer.
**One call per tick on a slow-due pass, never two** -- the full-estate result is a strict superset
of the active-only one, so a tick due for the slow cadence never also pays for a separate
fast-only call. Both cadences (and the cheap extra history call, when made) share one
instance-level backoff: a failure on any of them means the identical thing (this instance cannot
currently be reached/authenticated), so there is no value in tracking them as independent health
signals.

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

**Attribution correction, round 4 (2026-08-23, live evidence,
`prompts/2026-08-23-path-attribution-and-category-escape-hatch.md`).** `_update_preflight`'s
attribution used to be category -> queue only, dropping any transfer with no category (or an
unmapped one) before ever looking at where its bytes actually are -- forcing configuration for a
fact the filesystem already answers (a queue's own `remote_path` **is** the folder a client's
finished items land in, for a connector whose reported `content_path` sits there). Attribution is
now path-first: `content_path` matched against every enabled queue's `remote_path`
(`core/settle.py._client_content_path_matches`'s own component-boundary rule, reused rather than
reimplemented), falling back to the category mapping only for a transfer with no `content_path`
yet. **This does not make the category mapping optional for every connector** -- rTorrent reports
its own *seeding* directory as `content_path` (spec §1.1), a different tree from a queue's
`remote_path` under the common hardlink layout, so path attribution essentially never fires for
it and the category mapping remains its only route. See `docs/download-client-framework-spec.md`
§8.3's own round-4 correction for the fuller reasoning, including the open question this
correction surfaced (an uncategorised rTorrent torrent with a non-matching path has *no*
attribution route at all, silently, and this task deliberately left that behaviour unchanged --
`docs/decisions.md`).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime

import aiosqlite

from lftpweb.core import audit
from lftpweb.core.clients import get_client_class
from lftpweb.core.clients.base import Operation
from lftpweb.core.clients.errors import ClientAuthenticationFailed, ClientError, ClientUnreachable
from lftpweb.core.clients.models import Transfer, TransferPhase
from lftpweb.core.crypto import DecryptionError, decrypt_secret
from lftpweb.core.preflight import PreflightHold, PreflightRow
from lftpweb.core.settle import _client_content_path_matches

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    """`api/settings_clients.py`'s own `_now_iso`, duplicated rather than imported -- a `core/`
    module importing from `api/` would run the dependency direction backwards (DESIGN.md §12's
    layering), and this is a four-line timestamp helper, not something worth a shared-utility
    module for.
    """
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


# --- Two cadences (spec §9.1, corrected this stage -- see module docstring) --------------------

# Matches `core/arrsync.py.ArrSettings`'s own current default (10s, 2026-08-21's issue #16) --
# this is the cadence the settle-gate skip, the withhold gate (#18's stage 3), and Preflight all
# need. On a non-slow-due tick, this drives `list_transfers(active_only=True)` always, plus
# `list_history()` too for any connector whose `Operation.LIST_HISTORY` is declared `NATIVE`
# (module docstring). Not settings-driven (unlike the *arr's own cadence) -- this task's scope is
# the poller and its cache, not a Settings UI knob nobody has asked for yet; a fixed constant is
# the honest reflection of that until real use says otherwise.
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


@dataclass(frozen=True)
class UnattributedClientInfo:
    """One line of the Preflight box's unattributed-clients banner (finding #2, widened
    2026-08-23 round 4 on live evidence: the banner said *that* a client had unattributable
    items but never *which* categories to go map, leaving a user with `ar-tv` already mapped no
    way to tell what else needed one). `unattributed_clients` below is this dataclass's one
    producer; `api/jobs.py.get_preflight` is its one consumer.

    `categories` -- sorted, distinct category names seen among this pass's unattributable items,
    **excluding** "no category at all" (that's `no_category_count`'s own job, a different
    problem with a different fix: "map this category" vs. "this client isn't labelling its
    downloads"). `count` is the same total this class's fields are a breakdown of, not a second,
    possibly-inconsistent measurement.
    """

    instance_id: int
    name: str
    count: int
    categories: tuple[str, ...]
    no_category_count: int


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

# --- The Preflight phase filter (spec §9.2) -- an allowlist, not a denylist -----------------
#
# 2026-08-23 (findings #12/#4): the original filter excluded `COMPLETED`/`FAILED` and admitted
# everything else, on the stated assumption that "every connector's own `active_only=True`
# contract already excludes terminal transfers." True for SABnzbd, where a finished item leaves
# the queue for history. **False for rTorrent**, where a finished torrent stays in the active
# list and seeds indefinitely -- so `SEEDING` slipped through the denylist and every seeding
# torrent became a Preflight row. The same filter failed in the opposite direction at the same
# time: `PAUSED` was never excluded either, yet nothing added it to any allowlist-shaped
# reasoning, so a paused-but-incomplete transfer -- exactly Preflight's own definition of "work
# that is coming" if someone intervenes -- had no path to appear. Adding `SEEDING` to the
# denylist would have been the identical mistake one phase later: an open-ended enum behind a
# denylist admits every case nobody has thought about yet, by construction.
#
# Preflight's own definition (`core/preflight.py`'s module docstring): "something lftpweb
# already knows about but has no work to do on yet" -- i.e. work that is coming. Decided
# deliberately, phase by phase, over the closed nine-value `TransferPhase` enum:
#   QUEUED, DOWNLOADING  -- work plainly coming.
#   PAUSED               -- known-but-not-arriving-yet; this is finding #4's fix. A paused,
#                            incomplete transfer is arguably the single most useful thing
#                            Preflight can show, since nothing else in lftpweb would otherwise
#                            tell you it is stuck.
#   VERIFYING, EXTRACTING -- post-download steps still between the transfer and landing.
# Deliberately excluded, each for its own reason rather than by default:
#   SEEDING    -- nothing is coming; this is the estate, not incoming work. It belongs to Disk
#                 review's second pile (spec §11.1d) -- a routing error, not missing coverage.
#   COMPLETED  -- retirement-on-handover's job, not this filter's (out of this task's scope --
#                 do not fold handover behaviour into this allowlist).
#   FAILED     -- nothing is coming; stage 3's withhold gate is the surface for a failure.
#   UNKNOWN    -- spec §4.2: unknown never blocks anything, and it must not populate anything
#                 either -- a row asserting nothing helps nobody.
# A `TransferPhase` member added later belongs to neither list until a person decides which --
# `tests/test_clientsync.py::test_preflight_phase_allowlist_covers_every_transfer_phase` fails
# the moment the two sets stop covering the enum exactly, so that decision can never again be
# skipped by accident the way `SEEDING`'s was.
_PREFLIGHT_PHASES: frozenset[TransferPhase] = frozenset(
    {
        TransferPhase.QUEUED,
        TransferPhase.DOWNLOADING,
        TransferPhase.PAUSED,
        TransferPhase.VERIFYING,
        TransferPhase.EXTRACTING,
    }
)


def _attribution_sample(
    transfers: list[Transfer], path_queues: list[aiosqlite.Row]
) -> tuple[int, int]:
    """Part 3 of this task (2026-08-23,
    prompts/2026-08-23-category-tristate-and-exclusion.md): "derive the per-client 'do you even
    need this control' copy from OBSERVED attribution counts... never from client_type."
    Hardcoding "usenet doesn't need it, torrent does" would be exactly the client-name branching
    §4.4/§5.1 forbid -- this counts, over **every** transfer this pass reported (not just the
    Preflight-eligible-phase subset `_update_preflight`'s own loop filters to -- a seeding or
    just-completed transfer's `content_path` is just as informative a data point about whether
    this client's downloads land where a queue can already find them):

    - `sample_size` -- how many transfers had *something* to attribute at all (a `content_path`
      or a `category`; a transfer reporting neither is not a data point either way, so it's
      excluded from both numbers rather than silently counted as "didn't match").
    - `matched_by_path` -- of those, how many were resolved by path alone (`_client_content_path_
      matches` against an enabled queue's `remote_path`), needing no category mapping at all.

    `ClientsTab.tsx`'s relevance copy (`lib/clientAttribution.ts`) reads these two numbers
    straight -- SABnzbd's "12 of 12 matched by folder" and rTorrent's "0 of 2 matched" are the
    same sentence template over different observed counts, never a branch on `client_type`.
    """
    sample_size = 0
    matched_by_path = 0
    for transfer in transfers:
        if not transfer.content_path and not transfer.category:
            continue
        sample_size += 1
        if transfer.content_path and any(
            candidate["remote_path"]
            and _client_content_path_matches(candidate["remote_path"], transfer.content_path)
            for candidate in path_queues
        ):
            matched_by_path += 1
    return sample_size, matched_by_path


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
        # The settle-gate skip's own read (stage 2b of #18, `core/settle.py.
        # find_client_completion`) -- `instance_id -> name`, refreshed every `_process_instance`
        # call, so `completed_transfers` below can name the client instance in an audit event
        # without a second database round trip. Never pruned on its own (matching
        # `_last_slow_poll_at`/`_preflight_holds`'s own already-accepted non-pruning behavior in
        # this module) -- a stale entry for a since-removed instance costs nothing, since
        # `_enabled_instance_ids` below is what actually gates which ids `completed_transfers`
        # ever looks at.
        self._instance_names: dict[int, str] = {}
        # Findings #2/reinforcing observation (2026-08-23, prompts/2026-08-23-tilde-and-
        # visibility.md): instance id -> how many Preflight-eligible-phase transfers the most
        # recent pass saw that could not be attributed to any queue (no category reported, or a
        # category with no enabled mapping) -- `unattributed_clients` below's own source, and the
        # Preflight box's "this client reports N items, none attributable" banner's only input.
        # Refreshed every `_update_preflight` call, wholesale-replaced like every other per-
        # instance cache in this module (never merged across passes).
        self._unattributed_counts: dict[int, int] = {}
        # Round 4 of the same finding (live evidence, 2026-08-23): the banner's own category
        # breakdown for the count above -- instance id -> {category name (or `None` for "no
        # category at all"): count}. Refreshed alongside `_unattributed_counts` every
        # `_update_preflight` call, same wholesale-replace-per-pass shape.
        self._unattributed_categories: dict[int, dict[str | None, int]] = {}
        # Same finding: which instance ids have completed at least one successful poll *this
        # process's lifetime* -- the guard behind the one-time `client_poll_first_success` audit
        # event `_record_poll_result` below writes (never a per-poll event, this task's own
        # explicit rule). Reset on restart by construction (a fresh scheduler starts with an
        # empty set), which is accepted: a restart re-announcing "this instance is alive" once is
        # a fact worth a line in the log, not noise.
        self._ever_succeeded: set[int] = set()
        # This pass's own live "which instances did `run_once` actually consider" set (spec:
        # "a disabled instance is never contacted") -- refreshed at the top of every `run_once`,
        # *before* any per-instance processing, so `completed_transfers` can restrict itself to
        # it and a since-disabled instance's stale `_full_estate` entry can never satisfy the
        # settle-gate skip merely because nothing has overwritten it yet. Empty until the first
        # `run_once` call, which is exactly "nothing enabled yet" -- the same conservative
        # default every other cache in this module starts from.
        self._enabled_instance_ids: frozenset[int] = frozenset()

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
        # Stage 2b of #18: refreshed *before* any per-instance processing below (not merely
        # "eventually consistent with it"), so a `completed_transfers()` call racing this pass
        # -- from a concurrent scan, in production `AutoQueue.on_scan` and this loop share one
        # event loop but not one call stack -- never sees an instance this very pass is about to
        # skip (backed off, or freshly disabled) as still "enabled."
        self._enabled_instance_ids = frozenset(row["id"] for row in instances)
        for instance in instances:
            await self._process_instance(instance, now)

    async def _process_instance(self, instance: aiosqlite.Row, now: float) -> None:
        instance_id = instance["id"]
        instance_name = instance["name"]
        self._instance_names[instance_id] = instance_name

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
            # exactly as a fast-only result would have.
            last_slow = self._last_slow_poll_at.get(instance_id, 0.0)
            slow_due = now - last_slow >= self.SLOW_INTERVAL_S
            cheap_history = client.capabilities.supports(Operation.LIST_HISTORY)
            try:
                if slow_due:
                    transfers = await client.list_transfers(active_only=False)
                else:
                    # The corrected cheap/expensive split (module docstring, spec §9.1): always
                    # the active-only call, plus this connector's own cheap terminal-verdict
                    # source when it has one -- decided purely from `capabilities`, never from
                    # `client_type`.
                    transfers = await client.list_transfers(active_only=True)
                    if cheap_history:
                        transfers = transfers + await client.list_history()
            except ClientError as exc:
                await self._handle_failure(instance_id, instance_name, exc, now)
                return

            self._backoff.pop(instance_id, None)  # reachable (and authenticated) again
            await self._record_poll_result(instance_id, instance_name, ok=True, message=None)
            await self._update_preflight(instance, transfers, now)
            if slow_due:
                self._full_estate[instance_id] = {t.client_id: t for t in transfers}
                self._last_slow_poll_at[instance_id] = now
            elif cheap_history:
                # Merge just the terminal transfers this fast tick's own cheap history call
                # learned about into the full-estate cache -- the fix stage 2b's own correction
                # (spec §9.1) called for: a terminal verdict is now visible to
                # `completed_transfers()`/`failed_transfers()` within one `FAST_INTERVAL_S` tick,
                # not stranded behind `SLOW_INTERVAL_S`. Deliberately leaves every non-terminal
                # entry the last slow pass cached untouched -- this fast tick never asked about
                # those, and overwriting them with nothing would be a regression, not a fix.
                cache = self._full_estate.setdefault(instance_id, {})
                for transfer in transfers:
                    if transfer.phase in (TransferPhase.COMPLETED, TransferPhase.FAILED):
                        cache[transfer.client_id] = transfer
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
        # Every real attempt updates the row's own "last poll" status (finding #2) -- unlike the
        # audit event below, which stays transition-only on purpose. This is a single-row status
        # column, not a log; "credential rejected" vs "unreachable" is exactly `_FAILURE_VERB`'s
        # own wording, so the Clients page and the audit log can never say different things about
        # the same failure kind.
        await self._record_poll_result(
            instance_id, instance_name, ok=False, message=_FAILURE_VERB[kind]
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

    # --- Per-pass poll status (finding #2, 2026-08-23) --------------------------------------

    async def _record_poll_result(
        self, instance_id: int, instance_name: str, *, ok: bool, message: str | None
    ) -> None:
        """`download_client.last_poll_at`/`last_poll_ok`/`last_poll_message`/`last_success_at`
        (migration 029) -- written on **every** actual poll attempt, success or failure alike, so
        the Clients page can show what the poller most recently found rather than only what the
        last manual Test click found (this task's own "not just its last test" requirement). This
        is a single-row status column, not a log entry, so the audit log's own "one event per
        failure transition, not per failed pass" rule (`_handle_failure`'s own comment) does not
        apply here -- there is nothing to flood, only ever one row per instance.

        **The one exception is the positive signal itself**: the very first time this instance's
        poll succeeds during this process's lifetime (`self._ever_succeeded`), one
        `client_poll_first_success` audit event marks the transition from "never proven alive" to
        "working" -- a fact worth a line in the log exactly once, never a heartbeat (this task's
        own explicit "do not emit a per-poll event" instruction, honored by every *subsequent*
        successful pass writing nothing to the event log at all, only to this row).
        """
        now_iso = _now_iso()
        if ok:
            await self.db.execute(
                "UPDATE download_client SET last_poll_at = ?, last_poll_ok = 1, "
                "last_poll_message = NULL, last_success_at = ? WHERE id = ?",
                (now_iso, now_iso, instance_id),
            )
        else:
            await self.db.execute(
                "UPDATE download_client SET last_poll_at = ?, last_poll_ok = 0, "
                "last_poll_message = ? WHERE id = ?",
                (now_iso, message, instance_id),
            )
        await self.db.commit()
        if ok and instance_id not in self._ever_succeeded:
            self._ever_succeeded.add(instance_id)
            await audit.record_event(
                self.db,
                level="info",
                kind="client_poll_first_success",
                message=(
                    f"download client {instance_name!r} (id={instance_id}) reported its first "
                    "successful poll"
                ),
            )

    async def _record_attribution_stats(
        self, instance_id: int, sample_size: int, matched_by_path: int
    ) -> None:
        """Migration 031 (Part 3, this task) -- persists `_attribution_sample`'s own count for
        this pass. **Only called when `sample_size > 0`** (`_update_preflight`'s own call site) --
        a pass with nothing to attribute leaves the last informative reading on the row exactly
        as it was, the same "success writes, a quiet pass leaves the prior value alone" pattern
        `_persist_capabilities`/`_persist_detected_categories` already follow elsewhere in this
        codebase; overwriting a real "12 of 12" with a fabricated "0 of 0" during a temporary lull
        would make the relevance copy flicker for no reason.
        """
        await self.db.execute(
            "UPDATE download_client SET attribution_sample_size = ?, "
            "attribution_matched_by_path = ? WHERE id = ?",
            (sample_size, matched_by_path, instance_id),
        )
        await self.db.commit()

    # --- Preflight (spec §9.2) ----------------------------------------------------------------

    async def _category_queue_map(self, instance_id: int) -> dict[str, aiosqlite.Row]:
        """This instance's own category -> queue binding (spec §8.3), restricted to currently
        **enabled** queues -- a category mapped to a disabled queue is exactly as unattributable
        as one mapped to nothing at all. Fresh every pass, the same "no cache staleness on a
        config change" reasoning every other live query in this codebase's poller-adjacent code
        already follows.

        **Fallback only, since the §8.3 correction below** -- a transfer that already reports a
        `content_path` is attributed by path first (`_update_preflight`); this map is consulted
        only for a transfer with no path yet, which is the one case a category mapping still
        answers something the filesystem can't.
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

    async def _excluded_categories(self, instance_id: int) -> frozenset[str]:
        """This instance's own categories explicitly marked "not used by this instance"
        (migration 031, finding #15/#16, 2026-08-23) -- a *saved decision*, not merely the
        absence of a binding. Consulted by `_update_preflight` so a transfer in an excluded
        category is silently omitted **without** counting toward `unattributed_clients`'s own
        banner -- the whole point of the three-state redesign (finding #15: "the banner counts
        only undecided categories... a client whose every category is bound or explicitly
        excluded is fully configured and the banner must be silent").
        """
        cursor = await self.db.execute(
            "SELECT category FROM download_client_category " "WHERE client_id = ? AND excluded = 1",
            (instance_id,),
        )
        rows = await cursor.fetchall()
        return frozenset(r["category"] for r in rows)

    async def _enabled_queues(self) -> list[aiosqlite.Row]:
        """Every currently **enabled** queue's own `id`/`name`/`short_name`/`remote_path` --
        docs/download-client-framework-spec.md §8.3 correction (round 4, 2026-08-23): a
        transfer's own `content_path` is matched against *every* enabled queue's `remote_path`,
        not merely one this instance happens to have a category mapping for. That is the whole
        point of the correction -- path attribution needs no configuration on this instance at
        all, unlike `_category_queue_map` above, which is scoped to this instance's own bound
        categories by construction.
        """
        cursor = await self.db.execute(
            "SELECT id, name, short_name, remote_path FROM path_queue WHERE enabled = 1"
        )
        return await cursor.fetchall()

    async def _update_preflight(
        self, instance: aiosqlite.Row, transfers: list[Transfer], now: float
    ) -> None:
        """Project this pass's active transfers into Preflight rows (spec §9.2, "The Preflight
        source") and refresh this instance's own `PreflightHold`.

        **Attribution (spec §8.3 correction, round 4, 2026-08-23): path first, category as
        fallback.** A transfer's own `content_path` -- when the client has one to report --
        already answers "which queue does this belong to" with no configuration at all: a queue's
        `remote_path` **is** the on-disk root its finished items land under, so
        `core/settle.py._client_content_path_matches`'s own component-boundary rule (never a bare
        prefix -- `/complete/ar-tv` must not match `/complete/ar-tv-extra`, the same trap that
        rule already guards the settle-gate skip against) is reused here rather than reimplemented
        a second time. Order, exactly as decided:

        1. `content_path` matches an enabled queue's `remote_path` -> that queue. No category
           mapping consulted at all.
        2. Otherwise (most commonly: nothing on disk yet -- still queued at the client, no
           `content_path` to check), the configured category -> queue mapping, if one exists.
        3. Otherwise, **silently omitted** -- `core/preflight.py`'s own established rule,
           restated by the original handoff prompt: "promising a release that never arrives is
           worse than showing nothing."

        **Path wins on disagreement.** A transfer whose `content_path` matches one queue but
        whose category is mapped to a *different* one is not a tie -- the path is where the bytes
        actually are, a stale/wrong category mapping is not, and silently preferring the mapping
        would hide exactly the config error that needs fixing. Logged, not raised or blocked,
        since a mismatch here changes nothing about whether the transfer is shown, only which
        queue it's shown under.

        **This does not make the category mapping optional for every connector.** SABnzbd's
        history `storage` field lands inside the queue's own category folder, so path attribution
        covers it; rTorrent reports its own *seeding* directory as `content_path` (spec §1.1),
        which under the common hardlink layout is a different tree entirely from the queue's
        `remote_path` (the hardlinked completed-folder copy) -- so an rTorrent transfer's path
        essentially never matches a queue root, and the category mapping remains that
        connector's *only* attribution route, exactly as before this task. See this module's own
        docstring correction and the spec §8.3 correction (round 4) for the fuller reasoning --
        this is not a claim that most setups need this control less; it depends entirely on
        whether the connector's own `content_path` happens to sit under a queue's `remote_path`.

        **Excluded categories never reach the unattributed count** (finding #15/#16, 2026-08-23):
        a category explicitly marked "not used by this instance" is exactly the deployment shape
        this task exists for -- two lftpweb instances sharing one SABnzbd/rTorrent, each
        permanently seeing the other's work. Counting it toward `unattributed_clients`'s own
        banner would nag forever about work this instance is correctly ignoring, which is finding
        #15's own "a banner that cannot be resolved stops carrying information."
        """
        instance_id = instance["id"]
        category_map = await self._category_queue_map(instance_id)
        path_queues = await self._enabled_queues()
        excluded_categories = await self._excluded_categories(instance_id)

        seen: dict[str, PreflightRow] = {}
        unattributed = 0
        # finding #2's banner, widened (live evidence, 2026-08-23): *which* categories are going
        # unattributed, not merely how many -- `None` keys a transfer that reported no category
        # at all, a distinct problem from "reported a category with no mapping" (different fix).
        unattributed_categories: dict[str | None, int] = {}
        for transfer in transfers:
            # Allowlist, not denylist (2026-08-23, findings #12/#4 -- see `_PREFLIGHT_PHASES`'s
            # own comment for the full reasoning: a denylist admits every phase nobody thought
            # about by default, and one already had, in both directions at once).
            if transfer.phase not in _PREFLIGHT_PHASES:
                continue

            path_queue = None
            if transfer.content_path:
                for candidate in path_queues:
                    remote_path = candidate["remote_path"]
                    if remote_path and _client_content_path_matches(
                        remote_path, transfer.content_path
                    ):
                        path_queue = candidate
                        break
            category_queue = category_map.get(transfer.category) if transfer.category else None

            if path_queue is not None:
                queue = path_queue
                if category_queue is not None and category_queue["id"] != path_queue["id"]:
                    logger.warning(
                        "download client %d (%s): transfer %r content_path %r matches queue "
                        "%r by path, but category %r is mapped to queue %r -- path wins "
                        "(the category mapping is likely stale)",
                        instance_id,
                        instance["name"],
                        transfer.client_id,
                        transfer.content_path,
                        path_queue["name"],
                        transfer.category,
                        category_queue["name"],
                    )
            elif category_queue is not None:
                queue = category_queue
            else:
                # Finding #15/#16: a category the user has explicitly marked "not used by this
                # instance" is silently omitted **without** counting toward the unattributed
                # banner -- the whole point of the three-state redesign. A transfer with no
                # category at all is unaffected (that's `no_category_count`'s own, different,
                # problem).
                if transfer.category is not None and transfer.category in excluded_categories:
                    continue
                unattributed += 1
                key = transfer.category or None
                unattributed_categories[key] = unattributed_categories.get(key, 0) + 1
                continue

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

        self._unattributed_counts[instance_id] = unattributed
        self._unattributed_categories[instance_id] = unattributed_categories

        # Part 3 (2026-08-23): the observed attribution sample this pass produced -- over every
        # transfer reported, not just the Preflight-eligible-phase subset the loop above filters
        # to (see `_attribution_sample`'s own docstring for why). Only persisted when there was
        # something to observe; a pass with nothing to attribute leaves the last real reading in
        # place rather than fabricating a "0 of 0."
        sample_size, matched_by_path = _attribution_sample(transfers, path_queues)
        if sample_size > 0:
            await self._record_attribution_stats(instance_id, sample_size, matched_by_path)

        hold = self._preflight_holds.setdefault(instance_id, PreflightHold())
        # No `retired` set -- see this module's own docstring for why this source falls back to
        # the plain hold-then-expire path rather than `core/arrsync.py`'s active eviction.
        hold.update(seen, now=now)

    def unattributed_clients(
        self, enabled_instance_ids: frozenset[int]
    ) -> list["UnattributedClientInfo"]:
        """Finding #2 (2026-08-23): "a client with no category -> queue mapping contributes
        nothing, silently" -- one `UnattributedClientInfo` per currently-enabled instance whose
        most recent pass saw at least one Preflight-eligible-phase transfer it could not
        attribute to any queue. `api/jobs.py.get_preflight` turns this into the Preflight box's
        own banner, the mount-gate banner's own shape (one line per affected client, never one
        row per dropped item). `instance_id` (finding #13, 2026-08-23) rides along so that banner
        line can deep-link straight to this specific instance rather than naming a settings path
        for the user to navigate by hand.

        **Widened with a category breakdown (live evidence, 2026-08-23, round 4).** The count
        alone told a user *that* a client had unattributable items, never *which* categories to
        go map -- "reports 2 items, none attributable" with a client that already has `ar-tv`
        mapped leaves the user guessing what else needs mapping. `categories` is every distinct
        category name seen among this pass's unattributable items (sorted, so the banner's text
        is stable across passes with the same set); `no_category_count` is counted separately
        because "the client reported no category at all" and "reported a category with no
        mapping" are different problems with different fixes -- conflating them would send a
        user chasing a category mapping that was never the issue.

        **`0` is never included.** A client currently contributing nothing because it genuinely
        has nothing incoming right now is not the same fact as one silently dropping real items,
        and showing a permanent "0 unattributable" line for every quiet client would bury the
        one that actually needs attention -- the same "only surface what's actually wrong"
        instinct `AutoQueue.gated`'s own banner already follows.

        `enabled_instance_ids` here is deliberately **not** the same set `preflight_rows` takes
        (a bound, enabled category -> queue mapping) -- the whole point of this banner is to
        catch an instance with *no* such mapping at all, so the caller must pass every enabled
        instance id, mapped or not.
        """
        out: list[UnattributedClientInfo] = []
        for instance_id in enabled_instance_ids:
            count = self._unattributed_counts.get(instance_id, 0)
            if count > 0:
                breakdown = self._unattributed_categories.get(instance_id, {})
                categories = tuple(sorted(c for c in breakdown if c is not None))
                no_category_count = breakdown.get(None, 0)
                out.append(
                    UnattributedClientInfo(
                        instance_id=instance_id,
                        name=self._instance_names.get(instance_id, f"instance {instance_id}"),
                        count=count,
                        categories=categories,
                        no_category_count=no_category_count,
                    )
                )
        return out

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

    # --- The settle-gate skip's and withhold gate's own reads (stage 2b and stage 3 of #18,
    # docs/download-client-framework-spec.md §14) -- `core/autoqueue.py.on_scan` is the one
    # caller of each, gated by its own independent setting (`settle.SettleSettings.
    # client_skip_enabled`, `autoqueue.WithholdSettings.enabled`). ---------------------------

    def completed_transfers(self) -> list[tuple[int, str, Transfer]]:
        """Every currently cached **terminal, history-derived `COMPLETED`** transfer across
        every currently enabled instance's full-estate cache (`_full_estate`, populated by
        spec §9.1's slow cadence) -- `core/settle.py.find_client_completion`'s own candidate
        list, paired with the instance id and name an audit event needs to name the client
        instance that permitted a skip (this task's own explicit requirement).

        **Restricted to `_enabled_instance_ids`**, this pass's own live enabled set (refreshed
        at the top of every `run_once`, before this method could ever be called from a
        concurrent `AutoQueue.on_scan` mid-pass) -- so a since-disabled instance's stale
        `_full_estate` entry can never satisfy the gate merely because nothing has overwritten
        it since. Same "a disabled instance is never contacted" rule this class's own docstring
        states for polling, extended here to its cache.

        **Does not poll.** A pure read of whatever the slow cadence already cached; an instance
        whose slow poll hasn't run yet (freshly added, or still backing off after a failure)
        simply contributes nothing here, which is exactly spec §4.2's "absent is not a verdict"
        -- the caller's own fallback (run the settle gate as it runs today) is triggered by an
        empty return, never by this method raising or guessing.

        Only `TransferPhase.COMPLETED` transfers are ever included -- a queue-side (non-
        terminal) status must never satisfy the settle-gate skip (this task's own rule), and
        both connectors' own `map_phase` only ever produce `COMPLETED` from a history/terminal
        record in the first place (`sabnzbd.py`'s queue-status map has no `COMPLETED` entry at
        all; `rtorrent.py`'s only reaches it via `complete and not is_active`) -- this filter is
        therefore also a second, independent guard against the same mistake, not merely a
        restatement of it.
        """
        out: list[tuple[int, str, Transfer]] = []
        for instance_id in self._enabled_instance_ids:
            name = self._instance_names.get(instance_id, f"instance {instance_id}")
            for transfer in self._full_estate.get(instance_id, {}).values():
                if transfer.phase is TransferPhase.COMPLETED:
                    out.append((instance_id, name, transfer))
        return out

    def failed_transfers(self) -> list[tuple[int, str, Transfer]]:
        """`completed_transfers`'s mirror image, for the withhold gate (stage 3 of #18,
        docs/transfers-redesign-spec.md §4.3): every currently cached **terminal, explicit
        `FAILED`** transfer across every currently enabled instance's full-estate cache.

        Same shape, same guarantees, same reasons, as `completed_transfers` above -- restricted
        to `_enabled_instance_ids`, a pure read that never polls, and empty (never raising) for
        an instance whose slow poll (or fast cheap-history call, this stage's own fix) hasn't
        run yet. `core/settle.py.find_client_failure` is this method's own one caller's own
        candidate list, exactly as `find_client_completion` consumes this method's twin.

        Only `TransferPhase.FAILED` is ever included here -- a queue-side, non-terminal status
        must never satisfy the withhold gate (spec §4.2: "only an *explicit* failure blocks
        anything"), and an outright failure that never landed any bytes reports no
        `content_path` at all (both connectors' own history mapping), so it can never match
        anything downstream either -- "a client failing outright needs no code" (spec §4.3) is
        true by construction, not by a separate check here.
        """
        out: list[tuple[int, str, Transfer]] = []
        for instance_id in self._enabled_instance_ids:
            name = self._instance_names.get(instance_id, f"instance {instance_id}")
            for transfer in self._full_estate.get(instance_id, {}).values():
                if transfer.phase is TransferPhase.FAILED:
                    out.append((instance_id, name, transfer))
        return out
