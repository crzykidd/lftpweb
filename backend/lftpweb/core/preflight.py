"""The Preflight box's source-agnostic shape (docs/transfers-redesign-spec.md §4, prefigured;
this task's own handoff prompt, prompts/done/2026-08-20-preflight-box.md) -- "something lftpweb
already knows about but has no work to do on yet," independent of *which* upstream told it so.

**The *arr poller (`core/arrsync.py`) is the only source wired up so far.** A second is already
planned as an immediate follow-up: non-*arr items sitting in the settle gate
(`core/settle.py` -- a release still being uploaded to the seedbox, held until its remote
fingerprint holds still across two scans plus 60s). Those rows differ in an important way: they
*do* have a remote presence and a known remote size (they'll read something like "remote — 22
GB"), whereas an *arr queue record has no remote presence at all yet. **Nothing in this module
may name *arr, or any other single source, by construction** -- `PreflightRow.source` is the one
place a caller learns which source a row came from, and `source_label`/`source_kind`/
`status_label` are free-form, source-owned display text this module never interprets. Keeping
that boundary here (rather than baking *arr's own vocabulary into the shared row/cache shape) is
what lets the settle-gate follow-up add itself without reshaping anything this task ships.

`PreflightHold` is the flap-tolerance cache every source uses (only `core/arrsync.py` does, so
far) -- a row missing from a source's latest refresh for up to `PREFLIGHT_HOLD_S` keeps showing
rather than blinking out and back in. *Why* a row briefly stops being reported differs per
source -- a download client's own queue blanking out for a beat (the *arr's own SABnzbd
production incident, `core/arrsync.py`'s module docstring, 2026-08-18) for one source, a release
simply starting to transfer (and so leaving the settle gate) for another -- but the box's own
tolerance for a brief reporting gap is the same idea either way, so it lives here once rather
than being re-derived per source. A row not refreshed within the hold window is deleted from the
cache outright, never merely marked stale -- there is no persisted state and no further
escalation, so this can never itself become a second accumulation risk on top of whatever a given
source already guards against on its own.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

# Widened, not replaced, when a second source lands -- see this module's own docstring.
PreflightSource = Literal["arr"]


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


# Comfortably longer than a poll-driven source's own refresh cadence (the *arr poller's default
# is 60s, `core/arrsync.py.ArrSettings.poll_interval_s`) -- a couple of passes' worth of margin,
# so one missed refresh never blinks a row out and back. Short enough that a row genuinely gone
# clears within a couple of minutes, not indefinitely. One constant for the whole box, not one
# per source -- the tolerance a user should expect from "briefly not reported" is the same
# regardless of which upstream is doing the reporting.
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

    def update(self, seen: dict[str, PreflightRow], *, now: float) -> None:
        """`seen` is this refresh's complete set, keyed by identity -- every key present gets its
        entry refreshed (row data replaced, `last_seen` bumped to `now`); every key *not*
        present is held unless it has already been missing longer than `PREFLIGHT_HOLD_S`, in
        which case it is deleted outright. There is no third state.
        """
        for key, row in seen.items():
            self._entries[key] = _HoldEntry(row=row, last_seen=now)
        for key in [k for k in self._entries if k not in seen]:
            if now - self._entries[key].last_seen > PREFLIGHT_HOLD_S:
                del self._entries[key]

    def rows(self) -> list[PreflightRow]:
        return [entry.row for entry in self._entries.values()]
