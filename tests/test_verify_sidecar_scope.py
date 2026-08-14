"""`core/verify.py._find_sidecars`' search scope -- which `.sfv`/`.md5` files belong to an item.

Exists because of a live defect found 2026-08-14 during a screenshot session. For a **loose
top-level file**, `search_root` is `root.parent` -- which at the queue root *is the queue's entire
local root* -- and the search used `rglob`, so it descended into every sibling release directory.
A 4.3 GB single `.mkv` was verified against a twelve-volume rar `.sfv` belonging to an unrelated
release, reported `CORRUPT: 12 of 12 checked file(s) failed ... missing`, and (a `move` queue)
withheld the remote delete for entirely the wrong reason.

That instance landed safe -- a false `CORRUPT` withholds a delete. The mirror image does not: a
loose file whose accidental sidecar happens to list names that exist nearby would report
`VERIFIED` on evidence about different bytes, and verification is the only gate on an irreversible
remote delete (DESIGN.md §6/§7.3).
"""

from __future__ import annotations

from lftpweb.core.verify import _find_sidecars


def _write(path, text="dummy"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def test_a_loose_file_does_not_pick_up_a_siblings_sidecar(tmp_path):
    """The reported bug, in its exact shape: a loose `.mkv` at the queue root beside a release
    directory that carries its own `.sfv`.
    """
    loose = _write(tmp_path / "Show3.S01E01.2160p.mkv", "x")
    _write(tmp_path / "Show.1.S16E13" / "Show.1.s16e13.sfv", "file.rar 00000000\n")

    assert _find_sidecars(loose) == []


def test_a_loose_file_still_finds_its_own_sidecar_alongside_it(tmp_path):
    """The convention the docstring names and the reason the parent is searched at all."""
    loose = _write(tmp_path / "Show3.S01E01.2160p.mkv", "x")
    sidecar = _write(tmp_path / "Show3.S01E01.2160p.sfv", "Show3.S01E01.2160p.mkv 00000000\n")

    assert _find_sidecars(loose) == [sidecar]


def test_a_loose_file_ignores_a_sidecar_nested_below_its_own_directory(tmp_path):
    """Non-recursive, not merely "less recursive": one directory deep, no descent."""
    loose = _write(tmp_path / "Show3.S01E01.2160p.mkv", "x")
    _write(tmp_path / "Subs" / "nested.sfv", "whatever 00000000\n")

    assert _find_sidecars(loose) == []


def test_a_directory_item_still_searches_its_own_subtree(tmp_path):
    """Unchanged, and deliberately: a release's sidecar routinely sits a level down (beside the
    archives, or inside `Sample/`/`Subs/`), so `rglob` over the item's *own* subtree is correct.
    """
    release = tmp_path / "Show.1.S16E13"
    _write(release / "a.rar", "x")
    top = _write(release / "Show.1.s16e13.sfv", "a.rar 00000000\n")
    nested = _write(release / "Subs" / "subs.md5", "0  a.rar\n")

    assert _find_sidecars(release) == sorted([top, nested])


def test_a_directory_item_does_not_reach_outside_itself(tmp_path):
    """The containment half of the same rule -- a sibling's sidecar is not this item's evidence
    in either direction.
    """
    release = tmp_path / "Show.1.S16E13"
    _write(release / "a.rar", "x")
    _write(tmp_path / "Show.2.S21" / "other.sfv", "b.rar 00000000\n")
    _write(tmp_path / "loose.sfv", "c.rar 00000000\n")

    assert _find_sidecars(release) == []


def test_a_missing_path_returns_nothing_rather_than_raising(tmp_path):
    assert _find_sidecars(tmp_path / "nope" / "gone.mkv") == []
