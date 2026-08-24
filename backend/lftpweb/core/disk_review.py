"""The disk review scan (docs/download-client-framework-spec.md §11, stage 4 of #18) --
**review-only, deletes nothing**. Walks the clients' configured base paths over SSH,
reconciles against what the clients claim and what lftpweb itself is using, and produces three
labelled piles: debris (safe to review, selectable), the seeding estate (claimed, shown for
visibility only), and the unclaimed pile (finding #17, 2026-08-23 -- ownership genuinely
undeterminable, shown for visibility, gated off the ordinary select-and-remove flow, never
hidden). Stage 5 (the delete path) is not built here -- see this module's own
`run_scan`, which never calls `core/remote.py.RemoteConnectionPool.delete_path`.

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

from lftpweb.core import mount_sentinel
from lftpweb.core.pipeline_flight import item_pipeline_busy_subquery
from lftpweb.core.remote import HostConfig, RemoteConnectionPool, RemoteScanError

logger = logging.getLogger(__name__)

# Same instinct as `core/mount_sentinel.py`'s own grace period (spec §7.3): a release written
# moments ago that hasn't yet appeared in any client's list must never be proposed. Reusing the
# constant rather than inventing a second magic number for the same "give it a few minutes"
# idea.
DEFAULT_AGE_FLOOR_S = mount_sentinel.DEFAULT_GRACE_S


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
    """One transfer's `content_path`, from a client instance that answered successfully this
    pass. Only ever constructed for a client already known to be reachable this pass -- an
    unreachable client's *last* claims must never be trusted as current (spec §4.2), and the
    root-level gate above already excludes its declared paths from debris regardless.

    `category` (2026-08-23, live use, finding #16's own follow-up -- see `reconcile`'s own
    `excluded_categories_by_client` docstring) is the transfer's own reported category, `None`
    when the client didn't report one. It carries no attribution meaning by itself; it exists
    only so `reconcile` can drop a claim outright when its category is one this client has
    explicitly marked "not used by this instance" -- the per-file version of finding #16's
    exclusion, for exactly the shape a category can never be resolved to a path (rTorrent).
    """

    client_id: int
    client_name: str
    transfer_id: str
    transfer_name: str
    content_path: str
    category: str | None = None


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


@dataclass(frozen=True)
class BrokenSeed:
    """One `A - B` entry -- a client's own claimed `content_path` under a base path this scan
    actually walked, with nothing found there. Never reported for a claim whose root wasn't
    walked at all (spec: absent information is not a verdict) -- see `reconcile`'s own
    `_root_containing`.
    """

    client_id: int
    client_name: str
    transfer_id: str
    transfer_name: str
    content_path: str


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

    A **claimed** file's own transfer category already answers the ownership question directly
    (`reconcile`'s own `excluded_categories_by_client` filtering, dropping the claim outright) --
    a file claimed by an excluded category is dropped before any claim/candidate logic runs and
    never appears in *any* pile, `unclaimed` included. Only a file with **no claim at all**, under
    an ambiguous root, ever lands here -- the line between "known to be someone else's" (excluded,
    invisible) and "ownership genuinely unknown" (this pile) is the one this whole task exists to
    keep sharp.

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
    broken_seeds: tuple[BrokenSeed, ...] = ()
    skipped_base_paths: tuple[SkippedBasePath, ...] = ()
    # The third pile (finding #17, 2026-08-23) -- see `UnclaimedItem`'s own docstring for why
    # this replaced a bare count.
    unclaimed: tuple[UnclaimedItem, ...] = ()


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
) -> ReconciliationResult:
    """The whole reconciliation, pure. `unavailable_roots` is roots this scan already knows it
    cannot trust (an SSH walk failure, a fallback path unable to supply inodes, or a queue's
    failed mount-sentinel check -- `run_scan`'s job to populate, not this function's to
    discover) -- every one of them is excluded from both piles outright, same as a root gated
    off here for a missing contributor (spec §11.1a).

    `excluded_paths` (migration 031, finding #16, 2026-08-23) -- "not used by this instance" as
    a hard safety boundary, not merely a banner-silencing preference: a path (or sub-path) this
    scan must **never propose as debris and never treat as claimed-or-unclaimed at all**, because
    it belongs to a different lftpweb instance sharing this seedbox. Every entry under one of
    these is dropped before any candidate/claim logic below ever sees it -- the same containment
    filter `roots` already applies, at finer grain. `run_scan` is what resolves an excluded
    *category* into paths here wherever a client declares a `content`-kind base path to resolve
    it onto -- this function only ever consumes the resulting flat path list.

    `excluded_categories_by_client` (live use, 2026-08-23, follow-up to finding #16 --
    *"there are things in there in ar-tv that it doesn't show now"*) is the per-file counterpart
    for the client shape `excluded_paths` cannot reach: rTorrent's own `content_path` is its
    seeding directory, unrelated to any category folder, so an excluded category can never
    resolve to a path for it at all. A **claimed** file already carries the answer directly on
    its own claim (`ClientClaim.category`) -- no path arithmetic needed -- so a claim whose
    category is in this client's excluded set is dropped *before* any claiming logic below ever
    sees it, exactly as if that transfer had never reported a `content_path`. This is universal
    (every client, not only the ones `excluded_paths` can't reach) and strictly narrower than the
    old whole-base-path fail-closed suppression: a bound category's claims on the very same root
    are completely unaffected.

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

    **Finding #17's own line, kept sharp here:** a claim whose category is excluded is *known* to
    belong to the other lftpweb instance -- it must never land in `unclaimed` either, which is
    reserved for ownership that is genuinely *unknown*. So its `content_path` is folded into the
    same hard-exclusion set `excluded_paths` uses, **before** the claim itself is dropped below --
    a disk entry under it is removed from consideration entirely, exactly like a manually
    excluded path (finding #16), rather than falling through to "nobody claims this" and landing
    in the ambiguous-root fail-closed pile by accident.
    """
    roots = {_norm(r) for r in base_paths}
    excluded_categories_by_client = excluded_categories_by_client or {}
    ambiguous_roots: dict[str, str] = dict(debris_ambiguous_roots or {})
    claims = list(claims)

    def _category_excluded(claim: ClientClaim) -> bool:
        return claim.category is not None and claim.category in excluded_categories_by_client.get(
            claim.client_id, ()
        )

    excluded = {_norm(p) for p in excluded_paths} | {
        _norm(c.content_path) for c in claims if _category_excluded(c)
    }

    def _excluded(path: str) -> bool:
        return any(_is_under(path, ex) for ex in excluded)

    disk_entries = [
        e
        for e in disk_entries
        # Containment (spec §11.2): only entries under a *configured* root are ever
        # considered, defensively re-checked here even though `run_scan` should never hand
        # this function anything else. An excluded path is dropped the same way, before it can
        # ever become a claim, a debris candidate, an unclaimed item, or a seeding-estate entry
        # (finding #16, sharpened by finding #17 for the per-claim-category fallback above).
        if _norm(e.root) in roots and not _excluded(_norm(e.abs_path))
    ]

    # A claim in an excluded category is dropped outright -- never a claim at all, for any
    # client, not only the ones a category can't resolve to a path for (this function's own
    # docstring, `excluded_categories_by_client`). Its own `content_path` is already hard-excluded
    # above, so the file it named never reaches the disk-entry loop at all -- it does not fall
    # through to "unclaimed" the way a truly ownerless file does.
    claims = [c for c in claims if not _category_excluded(c)]

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
    claim_paths = [_norm(c.content_path) for c in claims]
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
        for claim, cp in zip(claims, claim_paths, strict=False):
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
        """
        abs_p = _norm(entry.abs_path)
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
        reason (in use, too fresh, or a root that's `unavailable`/missing-contributor rather than
        merely ambiguous) -- `unclaimed` must contain only what this specific gate actually caught.
        """
        root = _norm(entry.root)
        if root not in ambiguous_roots:
            return False
        abs_p = _norm(entry.abs_path)
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

    debris: list[DebrisCandidate] = []
    seeding_estate: list[SeedingEstateEntry] = []
    unclaimed: list[UnclaimedItem] = []

    for entry in disk_entries:
        if entry.is_dir:
            continue
        abs_p = _norm(entry.abs_path)
        claimed = _entry_claimed(entry)
        if claimed:
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
                        claimed_content_path=claim.content_path,
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

    # --- A - B: broken seeds -----------------------------------------------------------------
    broken_seeds: list[BrokenSeed] = []
    entries_by_root: dict[str, list[DiskEntry]] = defaultdict(list)
    for e in disk_entries:
        entries_by_root[_norm(e.root)].append(e)

    for claim, cp in zip(claims, claim_paths, strict=False):
        if _excluded(cp):
            # Finding #16: a claim under an excluded path is not this instance's business to
            # judge "present" or "broken" either way -- the whole point of the exclusion.
            continue
        root = _root_containing(cp, roots)
        if root is None or root in unavailable_roots:
            # Either never a scanned tree at all, or the walk itself failed there -- absent
            # information, not a verdict either way (spec §4.2's instinct, applied here).
            continue
        found = any(_is_under(_norm(e.abs_path), cp) for e in entries_by_root.get(root, []))
        if not found:
            broken_seeds.append(
                BrokenSeed(
                    client_id=claim.client_id,
                    client_name=claim.client_name,
                    transfer_id=claim.transfer_id,
                    transfer_name=claim.transfer_name,
                    content_path=claim.content_path,
                )
            )

    return ReconciliationResult(
        debris=tuple(debris),
        seeding_estate=tuple(seeding_estate),
        broken_seeds=tuple(broken_seeds),
        skipped_base_paths=tuple(
            SkippedBasePath(root=root, reason=reason) for root, reason in sorted(skipped.items())
        ),
        unclaimed=tuple(unclaimed),
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
class ScanOutcome:
    result: ReconciliationResult
    client_failures: tuple[ClientReportFailure, ...] = field(default_factory=tuple)


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

    cursor = await db.execute(
        "SELECT client_id, category FROM download_client_category WHERE excluded = 1"
    )
    excluded_categories_by_client_list: dict[int, list[str]] = defaultdict(list)
    for row in await cursor.fetchall():
        excluded_categories_by_client_list[row["client_id"]].append(row["category"])
    # `reconcile`'s own claim-filtering wants a per-client *set*, not the list shape
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
        "SELECT id, name, client_type, config_json, secret_enc FROM download_client WHERE enabled = 1"
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
            if not transfer.content_path:
                continue
            claims.append(
                ClientClaim(
                    client_id=client_id,
                    client_name=client_name,
                    transfer_id=transfer.client_id,
                    transfer_name=transfer.name,
                    content_path=transfer.content_path,
                    # 2026-08-23, live use -- see `ClientClaim`'s own docstring: the transfer's
                    # own category, so `reconcile` can drop a claim outright when it's in a
                    # category this client has explicitly marked "not used by this instance,"
                    # with no path arithmetic needed.
                    category=transfer.category,
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
    )
    return ScanOutcome(result=result, client_failures=tuple(failures))
