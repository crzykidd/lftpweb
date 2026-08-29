"""The disk review scan (docs/download-client-framework-spec.md §11, stage 4 of #18) --
**review-only, deletes nothing**. Walks the clients' configured base paths over SSH,
reconciles against what the clients claim and what lftpweb itself is using, and produces
labelled piles: debris (safe to review, selectable), the seeding estate (claimed, shown for
visibility only, each row carrying which category-attribution state its claim has), excluded
content (2026-08-24 -- unclaimed content under an excluded path, shown for visibility, never
selectable), and the unclaimed pile (finding #17, 2026-08-23 -- ownership genuinely
undeterminable, shown for visibility, gated off the ordinary select-and-remove flow, never
hidden). Alongside the piles, `reconcile()` also emits a `torrents` summary -- one row per
claim, carrying the client's own reported figures (ratio, uploaded bytes, seed time, ...) and
two disk-derived ones (`file_count`, `size_on_disk`) -- and `run_scan()` emits a `clients`
roster naming which instances reported this pass and what fields they declare. Stage 5 (the
delete path) is not built here -- see this module's own `run_scan`, which never calls
`core/remote.py.RemoteConnectionPool.delete_path`.

**The governing principle for this whole module, sharpened 2026-08-24 (see
`docs/decisions.md`): exclusion is a delete-safety boundary, not a visibility boundary.**
Findings #16 and #17 (2026-08-23) already established this for two of the paths through this
module -- a manually excluded path must be dropped from *delete authorization*, not from the
screen, and an ambiguous-root unclaimed file must be shown, not silently counted. This task
applies the same correction a third time, to the one path that still got it wrong: a claim
whose own category was marked "not used by this instance" used to be dropped outright, before
any claim/candidate logic ever saw it -- which meant the files it named fell through to
whichever pile the *absence* of a claim produced (nothing, if they were also under a resolved
exclusion path; the unclaimed pile, if the exclusion couldn't resolve to a path). **That claim
is now retained.** An excluded-category claim's files are still claimed, so they still land in
the seeding estate -- shown, tagged `attribution="excluded"`, and by construction unreachable
by the debris or unclaimed branches, which only ever run for a file with no claim behind it at
all. Nothing about *containment* changes: `is_authorized_delete_target` still receives the same
resolved excluded-path set and still refuses everything under it, unconditionally, whether or
not that content is now visible somewhere on the page.

**This module is split in two, on purpose.** `reconcile()` below is pure set math over three
inputs (spec §11.1's Set A/B/C) -- no SSH, no database, no clock but the one passed in --
because the two mistakes that make this feature dangerous (spec §11.1a, §11.1b) are exactly
the kind of thing a unit test can pin down completely, and a function that can't touch the
network is a function whose tests can't accidentally rely on one being up. `run_scan()` at the
bottom is the thin I/O shell: gather base paths, contact clients, walk the seedbox, then hand
everything to `reconcile()`.

**The two mistakes, restated as this module's own load-bearing invariants:**

1. **Set A is a union across every client that declares a given base path** (spec §11.1a) --
   never per-client. And if any declared contributor to a base path did not answer
   successfully this pass (unreachable, disabled, or simply never configured to be reachable),
   **no debris is proposed for that path at all.** SAB and rTorrent sharing the reference
   workflow's TV completed folder is the concrete case this exists for: a scan that only knew
   about SAB would see every rTorrent release there as unclaimed.
2. **Claiming is by inode, not path** (spec §11.1b). rTorrent's `content_path` is its seeding
   directory; the completed-folder hardlink is invisible to its own API. A file is claimed if
   *any* link to its inode falls inside a claimed tree, and a candidate is proposed only when
   *every* link to its inode is itself a candidate -- `nlink` greater than the number of links
   this scan actually found is an unaccounted-for link, and the file is never proposed.

**Set C reuses `core/pipeline_flight.py`'s existing predicate, not a second definition of
"busy."** `run_scan()` builds its in-use set from `item_pipeline_busy_subquery` (post-
processing / *arr / deferred source-delete) unioned with a plain `job.state IN ('queued',
'running')` read -- the identical two pieces `core/pipeline_flight.in_flight_expr` itself
combines, just assembled from a item-id-set query instead of a per-row `SELECT` expression,
because this module wants a *set of ids* to filter against, not a boolean column. There is
still exactly one place "is lftpweb using this item" is decided.
"""

from __future__ import annotations

import json
import logging
import posixpath
import time
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Literal

from lftpweb.core import mount_sentinel
from lftpweb.core.pipeline_flight import item_pipeline_busy_subquery
from lftpweb.core.remote import HostConfig, RemoteConnectionPool, RemoteScanError

logger = logging.getLogger(__name__)

# Same instinct as `core/mount_sentinel.py`'s own grace period (spec §7.3): a release written
# moments ago that hasn't yet appeared in any client's list must never be proposed. Reusing the
# constant rather than inventing a second magic number for the same "give it a few minutes"
# idea.
DEFAULT_AGE_FLOOR_S = mount_sentinel.DEFAULT_GRACE_S

# Migration 031's three-state category (spec §17.7, `download_client_category.excluded`/
# `queue_id`), copied onto a claim by `reconcile` purely for display -- see `ClientClaim.
# category`'s own docstring and `run_scan`'s widened category query. `reconcile` never branches
# on this value; the file it is attached to is placed by claim-vs-path logic alone, unconditioned
# on which of the three states this is.
Attribution = Literal["bound", "excluded", "undecided"]


# ================================================================================================
# Pure reconciliation -- inputs, outputs, and `reconcile()`/`freed_bytes()`. No I/O below this
# point until `run_scan()`.
# ================================================================================================


def _norm(path: str) -> str:
    """Normalize a path for comparison: strip a trailing slash (root `/` excepted). Not
    `posixpath.normpath` -- this module never wants to silently resolve a literal `..` that
    somehow ended up in a stored path; the paths compared here always come from `find -printf`
    or a client's own reported `content_path`, not user typing.
    """
    p = path.strip()
    if len(p) > 1 and p.endswith("/"):
        p = p.rstrip("/")
    return p


def _is_under(path: str, root: str) -> bool:
    """True if `path` *is* `root`, or sits anywhere beneath it. Both already `_norm`-ed."""
    return path == root or path.startswith(root + "/")


@dataclass(frozen=True)
class DiskEntry:
    """One file found under a scanned base path (Set B) -- `core/remote.py.RemoteConnectionPool.
    scan_with_inodes`'s own output, joined with which configured root it was found under.
    Directories are collected by the caller too (for completeness of the walk) but `reconcile`
    only ever proposes files; see the module docstring on why an empty leftover directory is a
    named, accepted gap rather than silently invented scope.
    """

    root: str  # one of `base_paths` passed to `reconcile`, already `_norm`-ed
    rel_path: str
    abs_path: str  # `root` + '/' + `rel_path`, already `_norm`-ed
    is_dir: bool
    size: int
    mtime: float
    inode: int | None
    nlink: int | None


@dataclass(frozen=True)
class BasePathContributor:
    """One `download_client_base_path` row -- which client instance declared which physical
    root. Not filtered by the client's own `enabled` flag here; `reconcile` itself is what
    turns "a contributor never reported this pass" (disabled, unreachable, or simply absent
    from `reachable_client_ids`) into "no debris for that root" (spec §11.1a).
    """

    base_path: str
    client_id: int


@dataclass(frozen=True)
class ClientClaim:
    """One transfer, from a client instance that answered successfully this pass. Only ever
    constructed for a client already known to be reachable this pass -- an unreachable client's
    *last* claims must never be trusted as current (spec §4.2), and the root-level gate above
    already excludes its declared paths from debris regardless.

    **`content_path` is `None`-able as of 2026-08-24** (this task, spec §11.4's own "surface the
    two things currently dropped silently"): `run_scan` used to skip a transfer outright when the
    client reported no path for it at all, which made it vanish from the review entirely even
    though the client's own figures for it (ratio, seed time, ...) were real. Such a claim can
    never match a disk entry -- `reconcile` keeps it out of the path/inode matching loop
    entirely -- but it still gets a row in `torrents`, with `file_count`/`size_on_disk` reported
    `None` (genuinely unknown, not zero) rather than being dropped.

    `category` (2026-08-23, live use, finding #16's own follow-up) is the transfer's own reported
    category, `None` when the client didn't report one. **As of 2026-08-24 it no longer drives
    whether the claim survives at all** -- see this module's own docstring for why an excluded
    claim is retained rather than dropped. It now only feeds `reconcile`'s own `attribution`
    tagging (`Attribution`, purely for display).

    Every field below `category` is 2026-08-24, this task's own widening (spec §11.4): optional,
    `None` whenever the connector's own `CapabilitySet` doesn't declare the equivalent `Field` --
    **never fabricated as `0`/`0.0`/`""`, ever** (a SABnzbd claim's `ratio`/`uploaded_bytes`/
    `seed_time_s` are always `None`, per `USENET_BASELINE`; a `0.00` sitting beside a real ratio
    is exactly the "guess dressed up as a fact" `SpaceInfo`'s own docstring warns against).
    `reconcile` never interprets any of these -- it copies them onto the `torrents` row verbatim,
    straight off the `Transfer` record `run_scan` already has in hand.
    """

    client_id: int
    client_name: str
    transfer_id: str
    transfer_name: str
    content_path: str | None
    category: str | None = None
    size_bytes: int | None = None
    uploaded_bytes: int | None = None
    ratio: float | None = None
    seed_time_s: int | None = None
    added_at: str | None = None
    raw_status: str | None = None
    phase: str | None = None


@dataclass(frozen=True)
class InUsePath:
    """One entry in Set C -- an item `core/pipeline_flight.py`'s predicate says lftpweb is
    still using. `abs_path` is `path_queue.remote_path` + '/' + `item.rel_path`.
    """

    item_id: int
    abs_path: str


@dataclass(frozen=True)
class DebrisCandidate:
    """One row in the debris pile -- on disk, claimed by no client, not in use by lftpweb, past
    the age floor, and (spec §11.1b) every link to its inode is itself exactly this.
    `link_paths` is every on-disk path sharing this file's inode (including itself) when
    `nlink > 1` -- empty for an ordinary single-link file -- and is what `freed_bytes` groups
    selections by so a partial selection of a linked file never over-reports what a delete
    would actually reclaim.
    """

    root: str
    rel_path: str
    abs_path: str
    size: int
    mtime: float
    inode: int | None
    nlink: int | None
    link_paths: tuple[str, ...] = ()


@dataclass(frozen=True)
class SeedingEstateEntry:
    """One on-disk file that *is* claimed -- shown for visibility (spec §11.1d: "a review page
    that omits the seeding estate would be answering a question nobody asked") but never
    selectable, and never counted in a freed-space total.

    **`claimed_transfer_id`/`claimed_transfer_name`/`claimed_content_path` added 2026-08-23**
    (finding #7: "it would be better to show Torrents and expand each torrent to see details like
    files etc") -- the claim's own torrent identity, lifted straight from the `ClientClaim`
    `reconcile()` already resolves per file (`_claim_for`), so the display layer can roll files up
    by torrent without `reconcile()` itself changing shape: inode accounting stays per-file (spec
    §11.1b), only this dataclass now also carries the *reason* a file was claimed, not just the
    fact that it was.

    **`attribution`/`claim_key` added 2026-08-24** (this task, spec §11.4): `attribution` is the
    claim's own `Attribution` state, copied verbatim (see `ClientClaim.category`'s own docstring
    -- this file's presence here is never conditioned on the value, only its *label* is);
    `claim_key` (`f"{client_id}:{transfer_id}"`) is what lets the frontend join a seeding-estate
    file row to its own row in `torrents` without a second lookup.
    """

    root: str
    rel_path: str
    abs_path: str
    size: int
    claimed_by_client_id: int
    claimed_by_client_name: str
    claimed_transfer_id: str
    claimed_transfer_name: str
    claimed_content_path: str
    attribution: Attribution
    claim_key: str


@dataclass(frozen=True)
class ExcludedContentEntry:
    """One on-disk file under an excluded path (`download_client_excluded_path`, or a category
    resolved onto one via `resolve_category_exclusion_paths`) with **no claim currently covering
    it** -- 2026-08-24, this task, spec §11.4's own "route excluded-path content into its own
    pile, never into debris."

    Before this task, an excluded path's entries were dropped from `disk_entries` before any
    pile logic ran at all, which was correct for *delete safety* but wrong for *visibility*
    (this module's own docstring, the governing principle): a claim can vanish (the other
    lftpweb instance's client removes its history entry, or the torrent itself is removed) while
    the bytes are still sitting there, and a scan that only ever excluded-and-hid could never
    show that this had happened (§17.7's own "latent data-loss path" -- the content becomes
    indistinguishable from ordinary debris the moment set A stops claiming it). This pile is
    exactly that moment, made visible instead of silent.

    **Never selectable, never debris, never counted toward a reclaim total.** `link_paths` is
    computed the same link-aware way `DebrisCandidate`'s is (`_link_group`), so a hardlinked
    file's size is not double-counted just because it happens to be shown rather than actionable.
    """

    root: str
    rel_path: str
    abs_path: str
    size: int
    excluded_path: str
    link_paths: tuple[str, ...] = ()


@dataclass(frozen=True)
class TorrentEntry:
    """One claim's own summary row -- 2026-08-24, this task, spec §11.4. Supersedes `BrokenSeed`
    entirely (a `BrokenSeed` is just a claim whose root was walked and whose own tree turned up
    empty -- `file_count == 0`, `missing_on_disk=True` here) and adds what `BrokenSeed` never
    carried: the client's own reported figures (`ClientClaim`'s new fields, passed through
    unverified) plus two disk-derived ones this dataclass alone can answer, `file_count` and
    `size_on_disk`.

    **`file_count`/`size_on_disk` are `None`, not `0`, whenever the claim's own root was never
    walked, or the claim itself never reported a `content_path`** -- absent information is not a
    verdict (spec §4.2's instinct, restated here for the third time this module leans on it).
    Only a claim whose root *was* walked, and whose own tree turned up genuinely empty, gets
    `file_count=0` and `missing_on_disk=True`.

    **`size_on_disk` answers a different question from `freed_bytes`, deliberately.** It counts
    an inode once per *torrent* (so a claim whose own tree happens to contain two hardlinked
    copies of the same file isn't double-counted) but, unlike `freed_bytes`, never requires every
    link to an inode to be present before counting it -- the ordinary rTorrent shape is a claim's
    seeding-directory copy with its sibling hardlink sitting in a completely different claim's
    tree (the completed folder), and answering "how big is this torrent" with `0` because the
    other copy lives elsewhere would be exactly the fabricated-looking figure this task's own
    `ClientClaim` docstring warns against, just computed instead of reported.

    `claim_key` matches `SeedingEstateEntry.claim_key` -- see that dataclass's own docstring.
    `client_name` is deliberately not carried here: the response's `clients` array is the section
    a torrent lives under, so repeating the name on every row would be the exact "repeated on
    every file row" waste this module's own `TorrentEntry`-vs-per-file split exists to avoid --
    here it is per-torrent instead of per-file, but the same instinct.
    """

    client_id: int
    transfer_id: str
    transfer_name: str
    content_path: str | None
    category: str | None
    attribution: Attribution
    size_bytes: int | None
    uploaded_bytes: int | None
    ratio: float | None
    seed_time_s: int | None
    added_at: str | None
    raw_status: str | None
    phase: str | None
    file_count: int | None
    size_on_disk: int | None
    missing_on_disk: bool
    claim_key: str


@dataclass(frozen=True)
class SkippedBasePath:
    """One configured root this scan did not walk **at all**, and why -- a genuine "we have no
    information," surfaced to the review page rather than silently absorbed, the same "name gaps,
    don't hide them" instinct every other guard in this codebase follows. Reserved for a root
    this scan truly never looked at (no host, credentials, a failed SSH walk, a missing
    contributor, a failed mount-sentinel check) -- see `UnclaimedItem` below for the different,
    narrower case of a root that *was* walked but whose debris pile is incomplete.
    """

    root: str
    reason: str


@dataclass(frozen=True)
class UnclaimedItem:
    """The third pile (spec §11.1d, finding #17, 2026-08-23) -- one **genuinely unclaimed** file
    under a root where some client's excluded category could not be resolved to a path (an
    excluded category with no `content`-kind base path to resolve onto -- rTorrent, spec §1.1).
    It might be the leftover of a transfer that belonged to a since-vanished, excluded-category
    claim, or it might be ordinary debris from an interrupted operation, or it might be **another
    lftpweb instance's content** (finding #16) -- there is no path arithmetic that can tell, and
    that is precisely why this pile exists rather than a silent count.

    **This replaces the earlier `SuppressedDebrisItem`, which counted these files but never
    surfaced them** (finding #17: *"content that exists and is never surfaced is
    indistinguishable from content that is not there"* -- the same failure as finding #2, applied
    to fail-closed instead of a stale banner). Fail-closed now means "never treat as debris
    without a human looking at it," not "never display" -- so this dataclass carries the same
    shape as `DebrisCandidate` (including `link_paths`, so the pile's own reclaim figure stays
    link-aware, spec §10.5) plus `reason`, the same string `SuppressedDebrisItem.reason` used to
    carry.

    A **claimed** file's own transfer category already answers the ownership question directly.
    **2026-08-24 correction:** it used to be that a file claimed by an excluded category was
    dropped before any claim/candidate logic ran, so it never appeared in *any* pile, `unclaimed`
    included -- correct for keeping it out of `unclaimed`, but wrong for hiding it altogether
    (this module's own governing principle). The claim is now retained, so that file lands in the
    **seeding estate** instead, tagged `attribution="excluded"` -- shown, but nowhere near this
    pile, because "claimed" is checked before "excluded path" in `reconcile`'s own per-entry
    order. Only a file with **no claim at all**, under an ambiguous root, ever lands here -- the
    line between "known to be someone else's" (claimed, shown in the seeding estate) and
    "ownership genuinely unknown" (this pile) is the one this whole task exists to keep sharp.

    **Not selectable by the ordinary select-and-remove flow that debris uses** -- see
    `DiskReviewPage.tsx` and this task's own `docs/decisions.md` entry for the gate stage 5 must
    implement before anything acts on this pile.
    """

    root: str
    rel_path: str
    abs_path: str
    size: int
    mtime: float
    inode: int | None
    nlink: int | None
    link_paths: tuple[str, ...] = ()
    reason: str = ""


@dataclass(frozen=True)
class ReconciliationResult:
    debris: tuple[DebrisCandidate, ...] = ()
    seeding_estate: tuple[SeedingEstateEntry, ...] = ()
    skipped_base_paths: tuple[SkippedBasePath, ...] = ()
    # The third pile (finding #17, 2026-08-23) -- see `UnclaimedItem`'s own docstring for why
    # this replaced a bare count.
    unclaimed: tuple[UnclaimedItem, ...] = ()
    # The fourth pile, 2026-08-24 (this task, spec §11.4) -- see `ExcludedContentEntry`'s own
    # docstring.
    excluded_content: tuple[ExcludedContentEntry, ...] = ()
    # One row per claim, 2026-08-24 (this task) -- see `TorrentEntry`'s own docstring. Supersedes
    # `broken_seeds` (retired): a broken seed is exactly `missing_on_disk=True` here.
    torrents: tuple[TorrentEntry, ...] = ()


def _root_containing(path: str, roots: Iterable[str]) -> str | None:
    """The longest configured root that contains `path`, or `None` if no configured root does
    -- a claim whose `content_path` was never one of the scanned trees at all is neither
    provably present nor provably broken; `reconcile` skips it rather than guessing.
    """
    best: str | None = None
    for root in roots:
        if _is_under(path, root) and (best is None or len(root) > len(best)):
            best = root
    return best


def reconcile(
    *,
    base_paths: Iterable[str],
    contributors: Iterable[BasePathContributor],
    reachable_client_ids: Iterable[int],
    disk_entries: Iterable[DiskEntry],
    unavailable_roots: Mapping[str, str],
    claims: Iterable[ClientClaim],
    in_use: Iterable[InUsePath],
    now_s: float,
    age_floor_s: float = DEFAULT_AGE_FLOOR_S,
    excluded_paths: Iterable[str] = (),
    excluded_categories_by_client: Mapping[int, frozenset[str]] | None = None,
    debris_ambiguous_roots: Mapping[str, str] | None = None,
    category_attribution_by_client: Mapping[int, Mapping[str, Attribution]] | None = None,
) -> ReconciliationResult:
    """The whole reconciliation, pure. `unavailable_roots` is roots this scan already knows it
    cannot trust (an SSH walk failure, a fallback path unable to supply inodes, or a queue's
    failed mount-sentinel check -- `run_scan`'s job to populate, not this function's to
    discover) -- every one of them is excluded from both piles outright, same as a root gated
    off here for a missing contributor (spec §11.1a).

    `excluded_paths` (migration 031, finding #16, 2026-08-23) -- "not used by this instance" as
    a hard **delete-safety** boundary. `run_scan` is what resolves an excluded *category* into
    paths here wherever a client declares a `content`-kind base path to resolve it onto -- this
    function only ever consumes the resulting flat path list. **2026-08-24 correction (this
    module's own governing principle): it is not, and was never meant to be, a visibility
    boundary.** An entry under one of these paths is no longer dropped before candidate/claim
    logic runs -- it is routed to its own `excluded_content` pile (or, if a claim still covers
    it, the seeding estate) instead of being silently absorbed. What it still guarantees,
    unconditionally, is that nothing under it is ever eligible for `debris`: see `_entry_eligible`
    and `_ambiguous_unclaimed` below, both of which refuse an excluded entry directly, and see
    `is_authorized_delete_target` (unchanged, still the seed of the future containment check) for
    the other half of the guarantee.

    `excluded_categories_by_client` (live use, 2026-08-23, follow-up to finding #16) used to drop
    a claim outright when its category was marked "not used by this instance" -- **2026-08-24:
    it no longer does.** See this module's own docstring for why the claim is retained instead.
    Kept in this signature, separately from `category_attribution_by_client` below, so a future
    behavioural use of category exclusion inside this function (should one ever be needed) has an
    unambiguous home rather than overloading the display-only map -- today this function reads it
    for nothing; the old fold-into-`excluded_paths` and claim-dropping it drove are both retired,
    made unnecessary by claim retention itself (a claimed file is now routed to the seeding
    estate structurally, by the per-entry order below, not by removing it from the claim list).

    `category_attribution_by_client` (2026-08-24, this task, spec §11.4) is the **display-only**
    counterpart: `run_scan`'s own widened `download_client_category` read, one of `"bound"`
    (`queue_id IS NOT NULL`), `"excluded"` (`excluded = 1`), or `"undecided"` (neither). This
    function copies a claim's own state onto every row it emits for that claim (`SeedingEstateEntry
    .attribution`, `TorrentEntry.attribution`) and never branches on the value -- a claim with no
    category, or a category absent from this map, reads `"undecided"`.

    `debris_ambiguous_roots` (same follow-up, narrowed further by finding #17, 2026-08-23) is the
    genuinely irreducible remainder: an **unclaimed** file under a root where some client's
    excluded category could not be resolved to a path. There is no claim to read a category off
    of for a file nobody currently claims -- it might be the leftover of a transfer that belonged
    to a since-vanished, excluded-category claim, or it might be genuine debris, and there is no
    way to tell which. **Never proposed as debris, but never hidden either** -- these files land
    in `unclaimed` (`UnclaimedItem`, shown as their own pile, not selectable by the ordinary
    debris flow) instead of being silently counted, which is finding #17's own correction: fail-
    closed means "never act without a human looking at it," not "never display." **The root is
    still walked and its seeding estate still populated normally**, unlike `unavailable_roots`,
    which skips the walk outright. This is the whole point of the fix: the old design conflated
    "cannot resolve a category to a path" with "cannot trust anything under this root at all,"
    which suppressed legitimate, already-claimed content that was never at risk.

    **Finding #17's own line, kept sharp here, now enforced structurally rather than by data
    manipulation.** A claim whose category is excluded is *known* to belong to the other lftpweb
    instance -- it must never land in `unclaimed`, which is reserved for ownership that is
    genuinely *unknown*. Before 2026-08-24 this was guaranteed by folding the claim's own
    `content_path` into `excluded_paths` ahead of dropping the claim, so the disk entry it named
    never reached the unclaimed branch. **That machinery is gone -- it is no longer needed.**
    With the claim retained, "claimed" is checked before "excluded path" or "ambiguous unclaimed"
    in the per-entry loop below, so a claimed file can never reach either of those branches at
    all, for any reason. The guarantee moved from data (a set an entry gets removed from) to
    control flow (a branch order an entry can't skip past) -- a simplification, not an equivalent
    restatement.
    """
    roots = {_norm(r) for r in base_paths}
    ambiguous_roots: dict[str, str] = dict(debris_ambiguous_roots or {})
    category_attribution_by_client = category_attribution_by_client or {}
    claims = list(claims)

    def _attribution_for(claim: ClientClaim) -> Attribution:
        if claim.category is None:
            return "undecided"
        return category_attribution_by_client.get(claim.client_id, {}).get(
            claim.category, "undecided"
        )

    def _claim_key(claim: ClientClaim) -> str:
        return f"{claim.client_id}:{claim.transfer_id}"

    excluded = {_norm(p) for p in excluded_paths}

    def _excluded(path: str) -> bool:
        return any(_is_under(path, ex) for ex in excluded)

    def _matching_excluded_path(path: str) -> str | None:
        best: str | None = None
        for ex in excluded:
            if _is_under(path, ex) and (best is None or len(ex) > len(best)):
                best = ex
        return best

    disk_entries = [
        e
        for e in disk_entries
        # Containment (spec §11.2): only entries under a *configured* root are ever considered,
        # defensively re-checked here even though `run_scan` should never hand this function
        # anything else. An excluded path is **no longer** dropped here (2026-08-24) -- see this
        # function's own docstring; it is routed by the per-entry loop below instead.
        if _norm(e.root) in roots
    ]

    contrib_by_root: dict[str, set[int]] = defaultdict(set)
    for c in contributors:
        contrib_by_root[_norm(c.base_path)].add(c.client_id)

    reachable = set(reachable_client_ids)
    skipped: dict[str, str] = dict(unavailable_roots)

    root_available_for_debris: dict[str, bool] = {}
    for root in roots:
        if root in skipped:
            root_available_for_debris[root] = False
            continue
        missing = contrib_by_root.get(root, set()) - reachable
        if missing:
            root_available_for_debris[root] = False
            skipped[root] = (
                f"client(s) {sorted(missing)} did not report successfully this pass "
                "(unreachable, disabled, or never configured to answer) -- spec §11.1a"
            )
        elif root in ambiguous_roots:
            # Narrower than `unavailable_roots` on purpose (this function's own docstring): the
            # walk already happened and the seeding estate is unaffected -- only an *unclaimed*
            # file here is kept out of debris and shown in `unclaimed` below instead, never folded
            # into `skipped_base_paths` (which would wrongly imply the whole root was never
            # looked at).
            root_available_for_debris[root] = False
        else:
            root_available_for_debris[root] = True

    # --- Set A: claimed paths and, via any disk entry found under one, claimed inodes ---------
    # A claim with no `content_path` at all (spec §11.4's own "surface the two things currently
    # dropped silently" -- `ClientClaim.content_path`'s own docstring) can never match a disk
    # entry and must never enter the path/inode matching loop below; it still gets a `torrents`
    # row further down, built directly from the claim rather than from anything matched here.
    path_claims = [c for c in claims if c.content_path is not None]
    claim_paths = [_norm(c.content_path) for c in path_claims]  # type: ignore[arg-type]
    claimed_paths: set[str] = set()
    claimed_inodes: set[int] = set()
    # Which claim is *responsible* for a claimed path/inode -- for the seeding-estate pile's
    # "claimed by" display only, never for the claimed/unclaimed decision itself. Tracked
    # separately for inode claims (spec §11.1b's whole point: an entry can be claimed *only*
    # via a sibling link elsewhere, with no claim ever naming this exact path directly -- the
    # completed-folder hardlink never appears in rTorrent's own claim tree at all).
    path_claimed_by: dict[str, ClientClaim] = {}
    inode_claimed_by: dict[int, ClientClaim] = {}
    for entry in disk_entries:
        abs_p = _norm(entry.abs_path)
        for claim, cp in zip(path_claims, claim_paths, strict=False):
            if _is_under(abs_p, cp):
                claimed_paths.add(abs_p)
                path_claimed_by.setdefault(abs_p, claim)
                if entry.inode is not None:
                    claimed_inodes.add(entry.inode)
                    inode_claimed_by.setdefault(entry.inode, claim)
                break

    def _claim_for(entry: DiskEntry) -> ClientClaim | None:
        abs_p = _norm(entry.abs_path)
        claim = path_claimed_by.get(abs_p)
        if claim is not None:
            return claim
        if entry.inode is not None:
            return inode_claimed_by.get(entry.inode)
        return None

    # --- Set C -----------------------------------------------------------------------------
    in_use_paths = {_norm(u.abs_path) for u in in_use}

    def _is_in_use(abs_p: str) -> bool:
        return any(_is_under(abs_p, u) for u in in_use_paths)

    # --- Inode grouping, across the *whole* walk -- a hardlink can sit under a different root
    # than its sibling (rTorrent's own working root vs. the shared completed folder), so this
    # is never scoped to one root at a time.
    by_inode: dict[int, list[DiskEntry]] = defaultdict(list)
    for e in disk_entries:
        if not e.is_dir and e.inode is not None:
            by_inode[e.inode].append(e)

    def _passes_age_floor(entry: DiskEntry) -> bool:
        return (now_s - entry.mtime) >= age_floor_s

    def _entry_claimed(entry: DiskEntry) -> bool:
        abs_p = _norm(entry.abs_path)
        return abs_p in claimed_paths or (entry.inode is not None and entry.inode in claimed_inodes)

    def _entry_eligible(entry: DiskEntry) -> bool:
        """Everything *except* the claimed check -- shared by the single-link path and the
        nlink-group "every link is itself a candidate" check (spec §11.1b).

        **The `_excluded` guard is load-bearing, added 2026-08-24.** Before this task, an
        excluded entry was dropped from `disk_entries` before this function (or `by_inode`) ever
        saw it, so there was nothing here to guard against. Now that excluded entries stay in the
        walk (so they can be shown in `excluded_content`), this check is what stops one from
        riding along into `debris` through the back door: `_link_group`'s "every link must itself
        be a candidate" check calls `is_candidate` (which is this function, for the debris case)
        against *every on-disk sibling of an entry's inode*, not just the entry the outer loop is
        currently looking at. Without this line, a debris-eligible file whose hardlink sibling
        happens to sit under an excluded path would see that sibling read as "eligible" (nothing
        else here checks exclusion), satisfy the all-links-are-candidates requirement, and get
        proposed as debris -- exactly the outcome this whole task's hard invariant forbids. An
        excluded sibling failing this check instead makes the *whole group* fail closed, which is
        the same conservative default `_link_group`'s own docstring already describes for an
        unaccounted-for link.
        """
        abs_p = _norm(entry.abs_path)
        if _excluded(abs_p):
            return False
        if _is_in_use(abs_p):
            return False
        if not root_available_for_debris.get(_norm(entry.root), False):
            return False
        return _passes_age_floor(entry)

    def _ambiguous_unclaimed(entry: DiskEntry) -> bool:
        """True when `entry` is an **unclaimed** file that would otherwise have been debris-
        eligible, except that its root is in `ambiguous_roots` -- the genuinely irreducible
        remainder this function's own docstring describes, shown as its own pile (finding #17)
        rather than hidden. Never true for an entry that fails eligibility for an unrelated
        reason (in use, too fresh, excluded, or a root that's `unavailable`/missing-contributor
        rather than merely ambiguous) -- `unclaimed` must contain only what this specific gate
        actually caught.

        **The `_excluded` guard is the same 2026-08-24 fix as `_entry_eligible`'s own**, for the
        identical reason: `_link_group` calls this function against every on-disk sibling of an
        inode, and an excluded sibling must never be able to satisfy that check -- an excluded
        path's own abs_path must never end up inside another item's `link_paths`, which would
        otherwise leak it into `unclaimed`'s reclaim figure despite never appearing in the pile
        itself.
        """
        root = _norm(entry.root)
        if root not in ambiguous_roots:
            return False
        abs_p = _norm(entry.abs_path)
        if _excluded(abs_p):
            return False
        if _is_in_use(abs_p):
            return False
        return _passes_age_floor(entry)

    def _link_group(
        entry: DiskEntry, is_candidate: Callable[[DiskEntry], bool]
    ) -> tuple[str, ...] | None:
        """Shared by the debris and unclaimed piles (spec §11.1b, extended to the unclaimed pile
        by finding #17: its own reclaim figure must be link-aware too, spec §10.5, not just
        debris's -- a naive sum would reintroduce exactly the lie that section exists to prevent).

        Returns `()` for an ordinary single-link file (nothing to group); `None` when an
        unaccounted-for link exists outside every scanned tree, or when any link in the group
        fails `is_candidate` -- the conservative default, never propose either pile from a
        partial or contaminated group (spec §11.1b: "every link must itself be a candidate, or
        none of them are"); otherwise the sorted tuple of every on-disk path sharing the inode.
        """
        if not (entry.nlink and entry.nlink > 1 and entry.inode is not None):
            return ()
        links = by_inode.get(entry.inode, [])
        if len(links) < entry.nlink:
            return None
        if not all(is_candidate(link) for link in links):
            return None
        return tuple(sorted(_norm(link.abs_path) for link in links))

    def _excluded_and_unclaimed(entry: DiskEntry) -> bool:
        """`_link_group`'s `is_candidate` for the `excluded_content` pile: every on-disk sibling
        of an inode must itself be unclaimed *and* excluded, or the whole group fails closed --
        the same conservative default `_entry_eligible`/`_ambiguous_unclaimed` apply above, for
        the identical reason (never let a hardlink group's mixed membership produce a pile
        member that overstates what is actually known).
        """
        return not _entry_claimed(entry) and _excluded(_norm(entry.abs_path))

    debris: list[DebrisCandidate] = []
    seeding_estate: list[SeedingEstateEntry] = []
    unclaimed: list[UnclaimedItem] = []
    excluded_content: list[ExcludedContentEntry] = []

    # Per-entry order (2026-08-24, this task, spec §11.4) -- written here because it is what
    # makes this whole task safe: **claimed always wins**, checked before exclusion, which is
    # checked before the ambiguous-unclaimed fallback, which is checked before debris eligibility.
    # A file whose claim happens to be under an excluded path (the ordinary steady state of
    # §17.7's shared seedbox: the other instance's client still lists its own active transfers)
    # is shown in the seeding estate, not `excluded_content` -- that pile is reserved for content
    # under an excluded path that **no current claim covers**, which is exactly the "the other
    # instance's client dropped its history entry, or the torrent was removed" moment that used
    # to be invisible (§17.7's own "latent data-loss path"). Because "claimed" is checked first,
    # unconditionally, a claimed file can never reach `excluded_content`, `unclaimed`, or `debris`
    # -- this is what retired the old fold-into-hard-exclusion machinery (see this function's own
    # docstring): the guarantee it used to provide by removing an entry from consideration is now
    # provided by this order instead.
    for entry in disk_entries:
        if entry.is_dir:
            continue
        abs_p = _norm(entry.abs_path)
        claim = _claim_for(entry)
        if claim is not None:
            seeding_estate.append(
                SeedingEstateEntry(
                    root=_norm(entry.root),
                    rel_path=entry.rel_path,
                    abs_path=abs_p,
                    size=entry.size,
                    claimed_by_client_id=claim.client_id,
                    claimed_by_client_name=claim.client_name,
                    claimed_transfer_id=claim.transfer_id,
                    claimed_transfer_name=claim.transfer_name,
                    claimed_content_path=claim.content_path,  # type: ignore[arg-type]
                    attribution=_attribution_for(claim),
                    claim_key=_claim_key(claim),
                )
            )
            continue

        if _excluded(abs_p):
            group = _link_group(entry, _excluded_and_unclaimed)
            if group is not None:
                excluded_content.append(
                    ExcludedContentEntry(
                        root=_norm(entry.root),
                        rel_path=entry.rel_path,
                        abs_path=abs_p,
                        size=entry.size,
                        excluded_path=_matching_excluded_path(abs_p) or "",
                        link_paths=group,
                    )
                )
            continue

        if not _entry_eligible(entry):
            if _ambiguous_unclaimed(entry):
                group = _link_group(
                    entry,
                    lambda link: not _entry_claimed(link) and _ambiguous_unclaimed(link),
                )
                if group is not None:
                    unclaimed.append(
                        UnclaimedItem(
                            root=_norm(entry.root),
                            rel_path=entry.rel_path,
                            abs_path=abs_p,
                            size=entry.size,
                            mtime=entry.mtime,
                            inode=entry.inode,
                            nlink=entry.nlink,
                            link_paths=group,
                            reason=ambiguous_roots[_norm(entry.root)],
                        )
                    )
            continue

        group = _link_group(entry, lambda link: not _entry_claimed(link) and _entry_eligible(link))
        if group is None:
            continue

        debris.append(
            DebrisCandidate(
                root=_norm(entry.root),
                rel_path=entry.rel_path,
                abs_path=abs_p,
                size=entry.size,
                mtime=entry.mtime,
                inode=entry.inode,
                nlink=entry.nlink,
                link_paths=group,
            )
        )

    # --- `torrents`: one row per claim (2026-08-24, this task, spec §11.4) -------------------
    # Supersedes the old `broken_seeds` list entirely -- see `TorrentEntry`'s own docstring.
    entries_by_root: dict[str, list[DiskEntry]] = defaultdict(list)
    for e in disk_entries:
        entries_by_root[_norm(e.root)].append(e)

    def _size_on_disk(found: list[DiskEntry]) -> int:
        """Counts an inode once per torrent -- see `TorrentEntry`'s own docstring for why this
        is deliberately *not* `freed_bytes`'s "every link must be present" rule: a torrent's own
        footprint is a fact about its files, not a precondition on what else would need to be
        deleted alongside them.
        """
        total = 0
        seen_inodes: set[int] = set()
        for e in found:
            if e.inode is not None:
                if e.inode in seen_inodes:
                    continue
                seen_inodes.add(e.inode)
            total += e.size
        return total

    torrents: list[TorrentEntry] = []
    for claim in claims:
        file_count: int | None = None
        size_on_disk: int | None = None
        missing_on_disk = False
        if claim.content_path is not None:
            cp = _norm(claim.content_path)
            root = _root_containing(cp, roots)
            if root is not None and root not in unavailable_roots:
                # The root was walked -- absent information is not a verdict (spec §4.2), but
                # present information is, even for a claim under an excluded path: 2026-08-24,
                # this task's own "excluded claim can now be reported missing too. That is
                # correct -- it is visibility, which is the point" (see this module's own
                # docstring). There is no `_excluded(cp)` skip here any more.
                found = [
                    e
                    for e in entries_by_root.get(root, [])
                    if not e.is_dir and _is_under(_norm(e.abs_path), cp)
                ]
                file_count = len(found)
                size_on_disk = _size_on_disk(found)
                missing_on_disk = file_count == 0
        torrents.append(
            TorrentEntry(
                client_id=claim.client_id,
                transfer_id=claim.transfer_id,
                transfer_name=claim.transfer_name,
                content_path=claim.content_path,
                category=claim.category,
                attribution=_attribution_for(claim),
                size_bytes=claim.size_bytes,
                uploaded_bytes=claim.uploaded_bytes,
                ratio=claim.ratio,
                seed_time_s=claim.seed_time_s,
                added_at=claim.added_at,
                raw_status=claim.raw_status,
                phase=claim.phase,
                file_count=file_count,
                size_on_disk=size_on_disk,
                missing_on_disk=missing_on_disk,
                claim_key=_claim_key(claim),
            )
        )

    return ReconciliationResult(
        debris=tuple(debris),
        seeding_estate=tuple(seeding_estate),
        skipped_base_paths=tuple(
            SkippedBasePath(root=root, reason=reason) for root, reason in sorted(skipped.items())
        ),
        unclaimed=tuple(unclaimed),
        excluded_content=tuple(excluded_content),
        torrents=tuple(torrents),
    )


def freed_bytes(
    candidates: Iterable[DebrisCandidate | UnclaimedItem], selected_abs_paths: Iterable[str]
) -> int:
    """The link-aware reclaim total (spec §10.5, §11.1b's "same inode map produces both
    answers"). A linked file's bytes are counted once, and only when the selection includes
    *every* one of `link_paths` -- selecting one of two hardlinked candidates without the other
    reclaims nothing, because the other link still holds the inode.

    Shared verbatim by the debris pile (a real selection) and the unclaimed pile (finding #17:
    passed the pile's own full set of `abs_path`s as `selected_abs_paths` to get an "if this were
    all dealt with" total -- the unclaimed pile has no partial-selection UI, but its reclaim
    figure must still be link-aware, never a naive sum, for the same reason debris's is).
    """
    selected = {_norm(p) for p in selected_abs_paths}
    counted_groups: set[tuple[str, ...]] = set()
    total = 0
    for c in candidates:
        abs_p = _norm(c.abs_path)
        if abs_p not in selected:
            continue
        if not c.link_paths:
            total += c.size
            continue
        if c.link_paths in counted_groups:
            continue
        if set(c.link_paths).issubset(selected):
            total += c.size
            counted_groups.add(c.link_paths)
    return total


def resolve_category_exclusion_paths(
    content_base_paths: Iterable[str], categories: Iterable[str]
) -> list[str]:
    """Finding #16's own resolution rule (migration 031, 2026-08-23): a category marked "not
    used by this instance" is a *convenience* that resolves into the enforceable primitive
    (`download_client_excluded_path`-shaped paths) wherever spec §1.1's reference layout holds --
    a category is a folder directly under a `content`-kind base path, `<base>/<category>`.

    **Only ever called with a client's own `content`-kind base paths.** `run_scan` is what
    decides whether any exist for a given client at all; when none do (rTorrent: its only
    declared base path is its seeding/`working` directory, unrelated to any category folder --
    spec §1.1), this function is never reached, and the caller fails closed instead (suppressing
    debris for that client's entire declared base path rather than guess at a path arithmetic
    can't produce -- see `run_scan`'s own comment for the fail-closed branch).
    """
    return sorted(
        {
            posixpath.join(_norm(base), category)
            for base in content_base_paths
            for category in categories
        }
    )


def _resolve_client_exclusions(
    manual_excluded_paths: Iterable[str],
    base_paths_by_client: Mapping[int, list[tuple[str, str]]],
    excluded_categories_by_client: Mapping[int, list[str]],
) -> tuple[set[str], dict[str, str]]:
    """The pure derivation step behind `run_scan`'s own excluded-paths gathering (migration 031,
    finding #16, narrowed 2026-08-23 by live use -- see `reconcile`'s own
    `debris_ambiguous_roots` docstring) -- split out so the fail-closed rule and the
    category-to-path resolution are each directly testable without a database or an SSH
    connection.

    Returns `(excluded_paths, fail_closed_roots)`: the full excluded-path set (every manually
    configured row, plus every excluded category resolved into a path wherever the owning client
    declares a `content`-kind base path to resolve it onto), and a `root -> reason` mapping for
    any base path a client's exclusion could **not** be resolved onto by path. `run_scan` passes
    the latter to `reconcile` as `debris_ambiguous_roots` -- **no longer merged into
    `unavailable_roots`** (that would skip the walk and hide legitimate, already-claimed content
    that was never at risk, exactly the live-use complaint this narrowing fixes). `reconcile`
    itself is what actually fails closed, and only for the genuinely ambiguous remainder: an
    *unclaimed* file under one of these roots, once every claimed file has already been resolved
    directly against its own transfer's category (`ClientClaim.category`, universal, no path
    arithmetic needed).
    """
    excluded_paths: set[str] = {_norm(p) for p in manual_excluded_paths}
    fail_closed_roots: dict[str, str] = {}
    for client_id, categories in excluded_categories_by_client.items():
        client_base_paths = base_paths_by_client.get(client_id, [])
        content_bases = [p for p, kind in client_base_paths if kind == "content"]
        if content_bases:
            excluded_paths.update(resolve_category_exclusion_paths(content_bases, categories))
        else:
            # No `content`-kind base path exists for this client at all (rTorrent's own base
            # path is its seeding/`working` directory, unrelated to any category folder, spec
            # §1.1) -- resolving the exclusion precisely is impossible for an *unclaimed* file
            # here (a claimed one is already handled directly via its own transfer's category,
            # `reconcile`'s own `excluded_categories_by_client` filtering).
            reason = (
                f"client {client_id}'s excluded categor"
                f"{'y' if len(categories) == 1 else 'ies'} {sorted(categories)!r} cannot be "
                "resolved to a path (no content-kind base path declared for this client) -- an "
                "unclaimed file here cannot be told apart from the leftover of a since-vanished "
                "claim in that category, so it is suppressed from debris rather than risk "
                "proposing someone else's data (spec §11, finding #16); claimed content is "
                "unaffected and still shown normally"
            )
            for path, _kind in client_base_paths:
                fail_closed_roots.setdefault(path, reason)
    return excluded_paths, fail_closed_roots


def is_authorized_delete_target(
    path: str, base_paths: Iterable[str], excluded_paths: Iterable[str]
) -> bool:
    """The seed of spec §10.2's future delete-containment check (stage 5, not yet built --
    gated per §14's staging table until this task's own fixes landed). A target is authorized
    only when it sits inside one of the declared base paths **and** outside every excluded path
    -- the second half is this task's own addition (finding #16): an excluded path must be
    "never inside the delete containment boundary," not merely absent from the debris pile.
    Pure and independently testable now, even though stage 5 itself doesn't call it yet.
    """
    p = _norm(path)
    roots = {_norm(r) for r in base_paths}
    excluded = {_norm(e) for e in excluded_paths}
    if any(_is_under(p, ex) for ex in excluded):
        return False
    return any(_is_under(p, r) for r in roots)


# ================================================================================================
# I/O shell -- gathers Set A/B/C over the network and the database, then calls `reconcile()`.
# Manual trigger only (spec §11.3); never scheduled, never called from a page load.
# ================================================================================================


@dataclass
class ClientReportFailure:
    client_id: int
    client_name: str
    reason: str


@dataclass
class ClientSummary:
    """One row per **enabled** `download_client` instance, 2026-08-24 (this task, spec §11.4) --
    the roster the review page sections by. `reachable`/`failure_reason` restate the same facts
    `ClientReportFailure`/`reachable_client_ids` already carry (kept alongside, unfolded, per
    this task's own "keep `client_failures` working" instruction -- see `run_scan`'s own comment
    for why both exist rather than one replacing the other).

    `capabilities` is a **display-only** `Field name -> support level` mapping (`"native"` /
    `"derived"` / `"none"`), read straight off `download_client.capabilities_json` -- the same
    JSON shape `api/settings_clients.py._capabilities_to_json` writes, parsed here without
    importing `core/clients/base.py`'s `CapabilitySet`/`Field`/`Support` at all: this module has
    no use for the full typed vocabulary, only the flat strings a frontend column-visibility
    decision needs (spec §17.2: "the UI is driven by the declaration, never by the client's
    name" -- this is what lets the disk-review table honor that rule too, not just Settings).
    Empty for a client never successfully probed (`capabilities_json IS NULL`).
    """

    client_id: int
    client_name: str
    client_type: str
    reachable: bool
    failure_reason: str | None
    capabilities: dict[str, str] = field(default_factory=dict)


@dataclass
class ScanOutcome:
    result: ReconciliationResult
    client_failures: tuple[ClientReportFailure, ...] = field(default_factory=tuple)
    # The per-client roster (2026-08-24, this task) -- see `ClientSummary`'s own docstring.
    clients: tuple[ClientSummary, ...] = field(default_factory=tuple)


async def _load_in_flight_ids(db, postprocess_in_flight_ids: frozenset[int]) -> set[int]:
    """Set C's own id set -- `job.state IN ('queued', 'running')` unioned with
    `item_pipeline_busy_subquery` (`core/pipeline_flight.py`), the identical two pieces
    `in_flight_expr` itself combines. No second definition of "busy" here.
    """
    ids: set[int] = set()
    cursor = await db.execute(
        "SELECT DISTINCT item_id FROM job WHERE state IN ('queued', 'running')"
    )
    ids.update(row[0] for row in await cursor.fetchall())
    cursor = await db.execute(item_pipeline_busy_subquery(postprocess_in_flight_ids))
    ids.update(row[0] for row in await cursor.fetchall())
    return ids


async def _load_in_use_paths(db, postprocess_in_flight_ids: frozenset[int]) -> list[InUsePath]:
    ids = await _load_in_flight_ids(db, postprocess_in_flight_ids)
    if not ids:
        return []
    placeholders = ",".join(str(int(i)) for i in ids)
    cursor = await db.execute(
        "SELECT item.id, item.rel_path, path_queue.remote_path FROM item "
        "JOIN path_queue ON path_queue.id = item.queue_id "
        f"WHERE item.id IN ({placeholders})"
    )
    rows = await cursor.fetchall()
    return [
        InUsePath(
            item_id=row["id"],
            abs_path=posixpath.join(row["remote_path"].rstrip("/"), row["rel_path"]),
        )
        for row in rows
    ]


def _field_capabilities_from_json(raw_json: str | None) -> dict[str, str]:
    """`ClientSummary.capabilities`'s own parser -- see that dataclass's docstring for why this
    reads the raw JSON directly rather than importing `core/clients/base.py`. Tolerant of a row
    that was never probed (`None`) or a shape this reader doesn't recognize (never raises; an
    unparseable blob degrades to "no declared capabilities" rather than failing the whole scan
    over a display concern).
    """
    if not raw_json:
        return {}
    try:
        data = json.loads(raw_json)
    except (TypeError, ValueError):
        return {}
    fields = data.get("fields", {})
    if not isinstance(fields, dict):
        return {}
    return {
        name: entry["support"]
        for name, entry in fields.items()
        if isinstance(entry, dict) and "support" in entry
    }


async def _mount_gated_roots(db, roots: set[str]) -> dict[str, str]:
    """Any configured root that is *exactly* a queue's `remote_path`, for a queue whose local
    mount-sentinel check currently fails -- "a failed check skips that queue/path entirely"
    (`core/mount_sentinel.py`'s own instinct, applied here rather than to auto-queue's local
    absence question). Exact match only, not prefix containment -- a documented simplification,
    see this task's own final report.
    """
    cursor = await db.execute("SELECT name, remote_path, local_path FROM path_queue")
    gated: dict[str, str] = {}
    for row in await cursor.fetchall():
        root = _norm(row["remote_path"])
        if root not in roots:
            continue
        if not mount_sentinel.check(row["local_path"]):
            gated[root] = f"queue {row['name']!r}'s local mount is not healthy (mount sentinel)"
    return gated


async def run_scan(
    *,
    db,
    pool: RemoteConnectionPool,
    host: HostConfig | None,
    get_client_class: Callable[[str], type],
    decrypt_client_secret: Callable[[str], dict],
    postprocess_in_flight_ids: frozenset[int] = frozenset(),
    now_s: float | None = None,
    age_floor_s: float = DEFAULT_AGE_FLOOR_S,
) -> ScanOutcome:
    """The manual-trigger entry point (spec §11.3). `get_client_class`/`decrypt_client_secret`
    are injected rather than imported directly so this stays testable the way `core/clientsync.
    py` already is -- see that module's own `_process_instance` for the identical secret-
    decrypt-then-construct shape this mirrors.

    Contacts every *enabled* `download_client` instance fresh (never the poller's cache --
    spec §11.1a's "has not reported successfully *this pass*" means this pass, not the last
    time the poller happened to succeed) and walks every distinct declared base path
    (`download_client_base_path`, regardless of the owning client's `enabled` flag -- a
    disabled contributor still blocks debris on a path it shares, spec §11.1a, until the row is
    fixed or removed).
    """
    now_s = now_s if now_s is not None else time.time()

    cursor = await db.execute(
        "SELECT id, path, client_id, kind FROM download_client_base_path ORDER BY id"
    )
    base_path_rows = await cursor.fetchall()
    contributors = [
        BasePathContributor(base_path=row["path"], client_id=row["client_id"])
        for row in base_path_rows
    ]
    roots = sorted({_norm(row["path"]) for row in base_path_rows})

    unavailable_roots: dict[str, str] = {}
    disk_entries: list[DiskEntry] = []

    # --- Excluded paths (migration 031, finding #16, 2026-08-23) -- "not used by this instance"
    # as a hard safety boundary. Manual rows (`download_client_excluded_path`) are the enforceable
    # primitive, used verbatim; an excluded *category* is a convenience resolved into paths here,
    # wherever the client declares at least one `content`-kind base path for the resolution to
    # land on (spec §1.1's `<base>/<category>` layout).
    cursor = await db.execute("SELECT path FROM download_client_excluded_path")
    manual_excluded_paths = [row["path"] for row in await cursor.fetchall()]

    base_paths_by_client: dict[int, list[tuple[str, str]]] = defaultdict(list)
    for row in base_path_rows:
        base_paths_by_client[row["client_id"]].append((_norm(row["path"]), row["kind"]))

    # 2026-08-24, this task -- widened from "just the excluded subset" (which is still exactly
    # what `excluded_categories_by_client_list`/`excluded_categories_by_client` below need, for
    # `_resolve_client_exclusions`) to all three of migration 031's states, so `reconcile` can tag
    # each claim's own `attribution` for display (`category_attribution_by_client`) without a
    # second query. `excluded = 1` and `queue_id IS NOT NULL` are mutually exclusive by the API
    # layer's own validator (031's own migration comment) -- `excluded` is checked first below
    # purely because it is this task's own new state, not because of any real precedence question.
    cursor = await db.execute(
        "SELECT client_id, category, excluded, queue_id FROM download_client_category"
    )
    category_rows = await cursor.fetchall()
    excluded_categories_by_client_list: dict[int, list[str]] = defaultdict(list)
    category_attribution_by_client: dict[int, dict[str, Attribution]] = defaultdict(dict)
    for row in category_rows:
        if row["excluded"]:
            state: Attribution = "excluded"
            excluded_categories_by_client_list[row["client_id"]].append(row["category"])
        elif row["queue_id"] is not None:
            state = "bound"
        else:
            state = "undecided"
        category_attribution_by_client[row["client_id"]][row["category"]] = state
    # `reconcile`'s own signature wants a per-client *set*, not the list shape
    # `_resolve_client_exclusions` (below) and its own tests already settled on.
    excluded_categories_by_client: dict[int, frozenset[str]] = {
        cid: frozenset(cats) for cid, cats in excluded_categories_by_client_list.items()
    }

    # `debris_ambiguous_roots` (live use, 2026-08-23, narrowing finding #16's own fail-closed
    # rule -- see `reconcile`'s own docstring) -- **no longer merged into `unavailable_roots`**.
    # The old merge skipped the walk entirely for a root like rTorrent's whose category can't
    # resolve to a path, which hid its seeding estate too, not just debris -- legitimate,
    # already-claimed content that was never at risk. `reconcile` itself now does the actual
    # fail-closed narrowing, scoped to just the unclaimed remainder.
    excluded_paths, debris_ambiguous_roots = _resolve_client_exclusions(
        manual_excluded_paths, base_paths_by_client, excluded_categories_by_client_list
    )

    if host is None:
        for root in roots:
            unavailable_roots[root] = "no seedbox host is configured"
    elif host.credentials_need_reentry:
        # `load_host_config` never raises on a decrypt failure -- it sets this flag instead
        # (`core/engine.py`'s own docstring). Treated exactly like "no host": attempting a
        # connection here would only raise `RemoteDeleteError`'s sibling,
        # `DecryptionNeededError`, which is not a `RemoteScanError` and must not be allowed to
        # propagate out of this loop uncaught.
        for root in roots:
            unavailable_roots[root] = "seedbox host credentials need re-entry"
    else:
        # Every declared root is walked here -- including one in `debris_ambiguous_roots`
        # (2026-08-23 narrowing, this function's own comment above): a category that can't
        # resolve to a path no longer skips the walk, only narrows what `reconcile` may propose
        # as debris from what the walk finds. `unavailable_roots` is still empty at this point in
        # this branch (host configured, credentials fine) -- only a per-root scan failure below,
        # or the mount-sentinel check after this loop, can still add to it.
        for root in roots:
            try:
                entries, _warning = await pool.scan_with_inodes(host, root)
            except RemoteScanError as exc:
                unavailable_roots[root] = f"remote scan failed: {exc}"
                continue
            for rel_path, remote_entry in entries.items():
                disk_entries.append(
                    DiskEntry(
                        root=root,
                        rel_path=rel_path,
                        abs_path=posixpath.join(root, rel_path),
                        is_dir=remote_entry.is_dir,
                        size=remote_entry.size,
                        mtime=remote_entry.mtime,
                        inode=remote_entry.inode,
                        nlink=remote_entry.nlink,
                    )
                )

    unavailable_roots.update(await _mount_gated_roots(db, set(roots)))

    cursor = await db.execute(
        "SELECT id, name, client_type, config_json, secret_enc, capabilities_json "
        "FROM download_client WHERE enabled = 1"
    )
    client_rows = await cursor.fetchall()

    reachable_client_ids: set[int] = set()
    claims: list[ClientClaim] = []
    failures: list[ClientReportFailure] = []

    for row in client_rows:
        client_id = row["id"]
        client_name = row["name"]
        try:
            client_class = get_client_class(row["client_type"])
        except KeyError as exc:
            failures.append(
                ClientReportFailure(client_id, client_name, f"unregistered client_type: {exc}")
            )
            continue

        secret: dict = {}
        if row["secret_enc"]:
            try:
                secret = decrypt_client_secret(row["secret_enc"])
            except Exception as exc:  # noqa: BLE001 - any decrypt failure means "cannot report"
                failures.append(ClientReportFailure(client_id, client_name, f"credentials: {exc}"))
                continue
        non_secret = json.loads(row["config_json"]) if row["config_json"] else {}
        client = client_class(config={**non_secret, **secret})
        try:
            transfers = await client.list_transfers(active_only=False)
        except Exception as exc:  # noqa: BLE001 - spec §4.2: any failure means "did not report"
            failures.append(ClientReportFailure(client_id, client_name, str(exc)))
            continue
        finally:
            aclose = getattr(client, "aclose", None)
            if aclose is not None:
                await aclose()

        reachable_client_ids.add(client_id)
        for transfer in transfers:
            # 2026-08-24, this task, spec §11.4 -- a transfer with no `content_path` used to be
            # skipped here outright, which dropped it from the review entirely even though the
            # client's own figures for it were real (`ClientClaim.content_path`'s own docstring).
            # It still becomes a claim -- `reconcile` keeps a path-less claim out of the
            # path/inode matching loop, so it can never affect any pile, but it still gets a
            # `torrents` row.
            claims.append(
                ClientClaim(
                    client_id=client_id,
                    client_name=client_name,
                    transfer_id=transfer.client_id,
                    transfer_name=transfer.name,
                    content_path=transfer.content_path or None,
                    # 2026-08-23, live use -- see `ClientClaim`'s own docstring: the transfer's
                    # own category, now used only for display attribution (`reconcile`'s own
                    # `category_attribution_by_client`), never to drop the claim.
                    category=transfer.category,
                    size_bytes=transfer.size_bytes,
                    uploaded_bytes=transfer.uploaded_bytes,
                    ratio=transfer.ratio,
                    seed_time_s=transfer.seed_time_s,
                    added_at=transfer.added_at,
                    raw_status=transfer.raw_status,
                    phase=transfer.phase,
                )
            )

    in_use = await _load_in_use_paths(db, postprocess_in_flight_ids)

    result = reconcile(
        base_paths=roots,
        contributors=contributors,
        reachable_client_ids=reachable_client_ids,
        disk_entries=disk_entries,
        unavailable_roots=unavailable_roots,
        claims=claims,
        in_use=in_use,
        now_s=now_s,
        age_floor_s=age_floor_s,
        excluded_paths=excluded_paths,
        excluded_categories_by_client=excluded_categories_by_client,
        debris_ambiguous_roots=debris_ambiguous_roots,
        category_attribution_by_client=category_attribution_by_client,
    )

    # The per-client roster (2026-08-24, this task) -- built from the same `client_rows`/
    # `reachable_client_ids`/`failures` the loop above already produced, restated per-row rather
    # than replacing `client_failures` (this task's own "keep `client_failures` working").
    failure_reason_by_client: dict[int, str] = {f.client_id: f.reason for f in failures}
    clients = tuple(
        ClientSummary(
            client_id=row["id"],
            client_name=row["name"],
            client_type=row["client_type"],
            reachable=row["id"] in reachable_client_ids,
            failure_reason=failure_reason_by_client.get(row["id"]),
            capabilities=_field_capabilities_from_json(row["capabilities_json"]),
        )
        for row in client_rows
    )

    return ScanOutcome(result=result, client_failures=tuple(failures), clients=clients)
