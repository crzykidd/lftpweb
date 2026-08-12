"""One pattern evaluator, used in two places (DESIGN.md §4.7, §12).

Three kinds, doing three different jobs:

| Kind           | Matches against              | Effect                              | Enforced by |
|----------------|-------------------------------|--------------------------------------|-------------|
| `select`       | the item's own name            | which items auto-queue picks up      | us (`core/autoqueue.py`) |
| `skip`         | the item's own name            | items auto-queue never picks up      | us |
| `file_exclude` | a file's own basename          | files never transferred at all       | lftp `--exclude-glob` |

This module is deliberately separate from `core/autoqueue.py` (which decides *when* to
evaluate) for exactly the reason DESIGN.md §12 calls out: the identical compiled
`file_exclude` set must build the lftp `--exclude-glob` arguments (`exclude_globs()`,
consumed by `core/queue.py`) *and* tell the reconciler what a directory is supposed to
contain (`build_counts_predicate()`, consumed by `core/reconcile.py`/`core/engine.py`). Two
copies of "what does this pattern match" drifting apart is precisely the bug that leaves
every filtered release stuck `PARTIAL` forever (§3.2 rule 8) — see docs/decisions.md.
"""

from __future__ import annotations

import fnmatch
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import aiosqlite

    from lftpweb.core.remote import RemoteEntry

# Characters that make a pattern a glob rather than a plain substring (DESIGN.md §4.7):
# "Glob (fnmatch) when the pattern contains *, ?, or [; plain substring otherwise." This is
# the dispatch rule, not SeedSync's "try substring OR glob on everything" — that's friendlier
# but ambiguous the instant a pattern contains a metacharacter (docs/decisions.md).
_GLOB_CHARS = frozenset("*?[")

VALID_KINDS = frozenset({"select", "skip", "file_exclude"})


def _is_glob(expr: str) -> bool:
    return any(c in _GLOB_CHARS for c in expr)


def pattern_matches(expr: str, name: str) -> bool:
    """One pattern against one name. Case-insensitive, always (§4.7)."""
    if _is_glob(expr):
        return fnmatch.fnmatch(name.lower(), expr.lower())
    return expr.lower() in name.lower()


@dataclass(frozen=True)
class Pattern:
    """One row of the `pattern` table (DESIGN.md §3.1), or an ad-hoc one for the live preview
    (`api/settings.py`'s pattern-preview endpoint), which never touches the database.
    """

    kind: str
    expr: str
    enabled: bool = True
    id: int | None = None
    queue_id: int | None = None


@dataclass(frozen=True)
class CompiledPatterns:
    """One queue's (or the preview form's) fully-resolved pattern set — global (`queue_id`
    `NULL`) patterns merged with that queue's own, enabled ones only. Immutable and cheap to
    build fresh every scan, which is what makes "adding a pattern retroactively re-evaluates
    the whole known model" (§4.7) simple: there is no cached compiled form to invalidate.
    """

    select: tuple[str, ...] = field(default_factory=tuple)
    skip: tuple[str, ...] = field(default_factory=tuple)
    file_exclude: tuple[str, ...] = field(default_factory=tuple)
    patterns_only: bool = False

    @classmethod
    def compile(
        cls, patterns: Iterable[Pattern], *, patterns_only: bool = False
    ) -> CompiledPatterns:
        # Materialize first: `patterns` may be a one-shot generator (the preview endpoint
        # passes one), and the three comprehensions below each need their own full pass —
        # iterating a generator three times would silently yield nothing on passes 2 and 3.
        patterns = list(patterns)
        select = tuple(p.expr for p in patterns if p.enabled and p.kind == "select")
        skip = tuple(p.expr for p in patterns if p.enabled and p.kind == "skip")
        file_exclude = tuple(p.expr for p in patterns if p.enabled and p.kind == "file_exclude")
        return cls(select=select, skip=skip, file_exclude=file_exclude, patterns_only=patterns_only)

    def file_excluded(self, basename: str) -> bool:
        """A `file_exclude` match against one file's own basename — used both for the
        reconciler's completeness predicate and, indirectly, for the loose-top-level-file
        intake rule below.
        """
        return any(pattern_matches(p, basename) for p in self.file_exclude)

    def item_matches(self, name: str, *, is_file: bool = False) -> bool:
        """select/skip evaluated against one *item's* own name (a top-level directory or a
        loose top-level file — DESIGN.md §4.7's "what counts as an item"). Skip beats select,
        evaluated after (§4.7). No select patterns ⇒ everything matches, unless
        *patterns-only* is on. When the item is itself a file, `file_exclude` is also tested
        against its name (§4.7's "file_exclude also applies to loose top-level files") —
        otherwise a `*.nfo` file_exclude suppresses nfos nested inside a release while
        happily auto-queueing a stray `notes.nfo` sitting at the queue root.
        """
        if any(pattern_matches(p, name) for p in self.skip):
            return False
        if is_file and self.file_excluded(name):
            return False
        if not self.select:
            return not self.patterns_only
        return any(pattern_matches(p, name) for p in self.select)

    def exclude_globs(self) -> tuple[str, ...]:
        """What to hand `lftp --exclude-glob` (`core/lftp.py.build_transfer_command`). A
        non-glob `file_exclude` pattern (e.g. `sample`) is wrapped `*sample*` so lftp's own
        glob matcher reproduces the same "plain substring" convenience `pattern_matches`
        gives everywhere else — lftp itself has no substring-match mode.
        """
        return tuple(p if _is_glob(p) else f"*{p}*" for p in self.file_exclude)


def build_counts_predicate(compiled: CompiledPatterns):
    """The seam `core/reconcile.py` left in phase 2 (`counts_predicate`), filled in here
    (DESIGN.md §3.2 rule 8, §4.7). A remote *file* counts toward its parent directory's
    completeness unless a `file_exclude` pattern matches its own basename — matched at any
    depth, top-level loose file or nested inside a directory alike, since `file_exclude`
    means "I don't want files like this," not "...only some of the places they might sit."
    Directories never fail this predicate themselves; `core/reconcile.py` only ever calls it
    for `RemoteEntry`s where `is_dir` is `False`.
    """

    def predicate(rel_path: str, entry: RemoteEntry) -> bool:  # noqa: ARG001
        basename = rel_path.rsplit("/", 1)[-1]
        return not compiled.file_excluded(basename)

    return predicate


async def load_patterns(db: aiosqlite.Connection, queue_id: int) -> list[Pattern]:
    """Every enabled pattern that applies to `queue_id` — its own plus every global
    (`queue_id IS NULL`) one (DESIGN.md §3.1/§4.7).
    """
    cursor = await db.execute(
        "SELECT id, queue_id, kind, expr, enabled FROM pattern "
        "WHERE enabled = 1 AND (queue_id IS NULL OR queue_id = ?) ORDER BY id",
        (queue_id,),
    )
    rows = await cursor.fetchall()
    return [
        Pattern(
            id=row["id"],
            queue_id=row["queue_id"],
            kind=row["kind"],
            expr=row["expr"],
            enabled=bool(row["enabled"]),
        )
        for row in rows
    ]


async def compiled_for_queue(
    db: aiosqlite.Connection, queue_id: int, *, patterns_only: bool = False
) -> CompiledPatterns:
    patterns = await load_patterns(db, queue_id)
    return CompiledPatterns.compile(patterns, patterns_only=patterns_only)
