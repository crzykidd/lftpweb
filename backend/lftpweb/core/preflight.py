"""The Preflight box's source-agnostic shape (docs/transfers-redesign-spec.md §4, prefigured;
this task's own handoff prompt, prompts/done/2026-08-20-preflight-box.md) -- "something lftpweb
already knows about but has no work to do on yet," independent of *which* upstream told it so.

**Two sources are wired up: the *arr poller (`core/arrsync.py`), and the settle gate's own
eligibility check (`core/autoqueue.py.AutoQueue`, added by
prompts/2026-08-20-preflight-waiting-sources.md).** A settle-gated row differs from an *arr row
in an important way: it *does* have a remote presence and a known remote size (it reads
"remote — 22 GB"), whereas an *arr queue record has no remote presence at all yet. **Nothing in
this module may name *arr, the settle gate, or any other single source, by construction** --
`PreflightRow.source` is the one place a caller learns which source a row came from, and
`source_label`/`source_kind`/`status_label` are free-form, source-owned display text this module
never interprets. Keeping that boundary here (rather than baking either source's own vocabulary
into the shared row/cache shape) is what let the settle-gate source add itself without reshaping
anything the first task shipped -- see `docs/decisions.md` for how that held up in practice.

`PreflightHold` is the flap-tolerance cache a source uses when its own report can go briefly
missing for reasons unrelated to the underlying fact changing -- `core/arrsync.py` is the one
user of it: a row missing from a poll for up to `PREFLIGHT_HOLD_S` keeps showing rather than
blinking out and back in, because a download client's own queue can blank out for a beat (the
*arr's own SABnzbd production incident, `core/arrsync.py`'s module docstring, 2026-08-18).
**Retirement is the one thing that skips the hold entirely** (2026-08-21, "a handed-over release
lingers in Preflight for up to 150s"): `update`'s own `retired` set is for a row a source knows is
gone for a *known* reason -- handed over to a real item -- as opposed to merely missing from this
one pass for a reason the source can't tell apart from a blip. See `update`'s own docstring for
the two-bucket split; the caller (`core/arrsync.py._preflight_candidates`) is where that
distinction is actually drawn, never here. **Not every source needs this.** `core/autoqueue.py`'s settle-gated rows are computed fresh from this
same process's own persisted state on every successful scan pass, with no external flakiness to
smooth over, so that source replaces its rows wholesale each pass instead of holding them here --
see that module's own "Preflight" section for why a full replace is strictly more correct for a
source with no flap risk to guard against. A row not refreshed within the hold window is deleted
from the cache outright, never merely marked stale -- there is no persisted state and no further
escalation, so this can never itself become a second accumulation risk on top of whatever a given
source already guards against on its own.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

# Widened, not replaced, when a new source lands -- see this module's own docstring.
PreflightSource = Literal["arr", "settle"]


@dataclass(frozen=True)
class PreflightRow:
    """One Preflight box row -- "something we know about but have no work to do on yet."
    Deliberately thin and source-agnostic: no id, no bytes-done, no queue position -- there is no
    `item` and no `job` behind a row here, on any source, and the handoff prompt's own "the rows
    are inert, and the box is what makes that structural" is exactly why nothing here invites a
    per-row control that would need one.
    """

    source: PreflightSource
    queue_id: int
    # The bound queue's own display identity (2026-08-21, "the columns moved around" fix) --
    # common to every source, so it lives here rather than being re-derived per source or, worse,
    # left off entirely (the defect this task fixes: a Preflight row had `queue_id` but no name,
    # so it couldn't show the queue tag every other row on the page shows). `queue_name` is the
    # queue's full `name`; `queue_short_name` mirrors `api/settings_queues.py.
    # resolve_queue_display_name`'s own `short_name` field (`None` for "no short name set") so
    # the frontend's existing `lib/queueDisplayName.ts.queueDisplayName(short, name)` fallback
    # renders this row's tag identically to every Transfers row's own tag, with no second
    # fallback rule invented here.
    queue_name: str
    queue_short_name: str | None
    title: str
    # Free-form, source-owned display text for "what state is this in" -- an *arr row's own
    # `trackedDownloadState` (e.g. `"downloading"`), a settle-gate row's own wording (e.g.
    # `"Settling"`). Never interpreted or branched on by this module.
    status_label: str | None
    # The upstream's own display name (an *arr instance's configured name, e.g. `"Sonarr"`) and,
    # when the source has one, a brand/variant hint for the row's own chip (`'sonarr'`/`'radarr'`
    # for *arr; `None` for a source with no logo of its own -- the settle gate, say). Rendering
    # owns the `source`/`source_kind` -> logo mapping; this module only ever carries the strings.
    source_label: str
    source_kind: str | None
    # A known total size and, when the source can compute one, how much is left to arrive --
    # both `None` when the source has neither (never a request to enrich one that lacks it, per
    # the handoff prompt's own instruction). An *arr row (still downloading) can populate both
    # from the *arr's own `size`/`sizeleft`; a settle-gate row (already fully present remotely,
    # just being confirmed stable) is expected to populate only `size_bytes`, from `item.
    # remote_size` -- there is nothing "left" from that source's own point of view.
    size_bytes: int | None
    size_remaining_bytes: int | None
    # How many seconds until this row's own source expects its wait to clear -- generic in name
    # (unlike `size_bytes`/`size_remaining_bytes` above, it happens to be populated by exactly
    # one source today, the same way those two happen to be populated by both): an *arr row
    # reads its own queue record's `timeleft` (`core/arrsync.py._parse_timeleft`); a settle-gated
    # row leaves this `None` -- the gate's remaining wait is bound by *scan count*, not a wall-
    # clock estimate this codebase has any business fabricating (`core/autoqueue.py`'s own
    # "Preflight" section), and its already-existing remaining figure is `size_bytes` above, not
    # a time. `None` for "the source has no meaningful estimate this pass" -- never a fabricated
    # or zero figure (the handoff prompt's own instruction), so a caller renders nothing rather
    # than a wrong number.
    remaining_s: float | None
    # The download client actually fetching this release, from the *arr's own point of view
    # (2026-08-21, user's own words: "tooltip maybe we should show the arr details. Downloading
    # from '<download client name>' from arr") -- an *arr row's own `downloadClient`; `None` for
    # a settle row (there is no separate download client in that source's own model -- lftpweb
    # *is* the thing fetching it) and for an *arr row whose response didn't happen to carry one.
    # Display-only provenance for a chip tooltip, never branched on.
    download_client: str | None
    # A generic "how far along has this row's own wait gotten" detail for the chip's own hover
    # tooltip (2026-08-21, "the settling chip should have a mouseover that shows time details") --
    # deliberately named after neither condition a caller might compute it from, the same way
    # `size_bytes`/`remaining_s` above are shared vocabulary rather than either source's own
    # words. A row whose wait is bound by *scan count* populates both: `wait_scans` is how many
    # consecutive matching observations have been made so far, `wait_since` is the ISO-8601
    # timestamp the current streak began at (`core/autoqueue.py.on_scan` populates both from
    # `core/settle.py`'s own `matched_scans`/`updated_at` pair). Rendered client-side through
    # `lib/format.ts.settleWaitLabel` -- the exact "Waiting for changes -- 1 of 2 scans, 35s of
    # 60s" wording the Files tree and the lifecycle R-icon tooltip already share, given the site-
    # wide scan-count/wall-clock constants (`GET /api/settings/settle`) that helper also needs --
    # so a third copy of that sentence is never written here or anywhere else. An *arr row's own
    # wait isn't bound by scan count at all (`remaining_s` above already covers what it can say),
    # so it leaves both `None` -- never a fabricated pair for a row with nothing to add. Both are
    # `None` together, or neither is; a caller never sees one set without the other.
    wait_scans: int | None
    wait_since: str | None


# Comfortably longer than a poll-driven source's own refresh cadence (the *arr poller's default
# is 10s as of 2026-08-21's issue #16, down from 60s -- `core/arrsync.py.ArrSettings.
# poll_interval_s`) -- more than a couple of passes' worth of margin at the new default (this
# constant was not lowered to match; see docs/decisions.md for why leaving it at 150s is the
# conservative call), so one missed refresh never blinks a row out and back. Short enough that a
# row genuinely gone clears within a couple of minutes, not indefinitely. One constant for the
# whole box, not one per source -- the tolerance a user should expect from "briefly not reported"
# is the same regardless of which upstream is doing the reporting.
PREFLIGHT_HOLD_S = 150.0


@dataclass
class _HoldEntry:
    row: PreflightRow
    last_seen: float  # a monotonic clock reading, per source-supplied convention (see below)


class PreflightHold:
    """A flap-tolerant cache of the most recent `PreflightRow` per identity, for one source's own
    scope (`core/arrsync.py` keeps one instance per bound *arr instance id). `update` is the only
    writer, called once per refresh with *every* row that source currently sees; `rows` is the
    only reader. The caller owns what "now" means (`time.monotonic()` throughout this codebase's
    own bounded-retry state, `core/arrsync.py`'s `_InstanceBackoff` and friends) and what counts
    as one identity (a download id, a settle-gate item id, ...) -- this class only manages the
    seen-vs-held-vs-expired bookkeeping once, so every source gets the identical flap tolerance
    without re-deriving it.
    """

    def __init__(self) -> None:
        self._entries: dict[str, _HoldEntry] = {}

    def update(
        self, seen: dict[str, PreflightRow], *, now: float, retired: Iterable[str] = ()
    ) -> None:
        """`seen` is this refresh's complete set, keyed by identity -- every key present gets its
        entry refreshed (row data replaced, `last_seen` bumped to `now`).

        Every key *not* present falls into one of two buckets (2026-08-21, the "a handed-over
        release lingers in Preflight for up to 150s" fix): `retired` is this refresh's own set of
        identities the source knows are gone for a *known* reason -- handed over to a real item,
        for `core/arrsync.py`'s own caller -- and those are deleted immediately, with no smoothing
        at all: there is nothing transient here to hold across. Anything else missing from `seen`
        and absent from `retired` is **merely absent** -- the source has no idea why, which is
        precisely the SABnzbd blank-queue blip this cache exists to absorb -- and keeps today's
        behaviour: held until it has been missing longer than `PREFLIGHT_HOLD_S`, then deleted.
        There is no third state, and a key never needs to appear in both `seen` and `retired` in
        the same call -- a row a source still sees this pass was not, by definition, just retired.
        """
        for key, row in seen.items():
            self._entries[key] = _HoldEntry(row=row, last_seen=now)
        retired_set = set(retired)
        for key in [k for k in self._entries if k not in seen]:
            if key in retired_set or now - self._entries[key].last_seen > PREFLIGHT_HOLD_S:
                del self._entries[key]

    def rows(self) -> list[PreflightRow]:
        return [entry.row for entry in self._entries.values()]

    def items(self) -> list[tuple[str, PreflightRow]]:
        """`rows()` widened with each row's own identity key (2026-08-21, "eviction latency"
        fix) -- for a caller that needs to re-test a held row against something keyed by that
        same identity (`core/arrsync.py.ArrSyncScheduler.preflight_rows`'s own request-time
        retirement re-check, against its last-seen `QueueRecord` per identity) without this
        class exposing its private `_entries` dict directly. Still says nothing about what an
        identity *means* -- that stays the caller's own business, per this module's own
        source-agnostic contract.
        """
        return [(key, entry.row) for key, entry in self._entries.items()]
