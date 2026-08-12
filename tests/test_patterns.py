"""Unit tests for `core/patterns.py` (DESIGN.md §4.7, §14's "pattern evaluator" bullet)."""

from __future__ import annotations

from lftpweb.core.patterns import CompiledPatterns, Pattern, build_counts_predicate, pattern_matches
from lftpweb.core.remote import RemoteEntry


# --- Glob-vs-substring dispatch, case-insensitivity (§4.7) ------------------------------


def test_plain_pattern_matches_as_substring_not_requiring_wildcards():
    assert pattern_matches("1080p", "Some.Show.1080p.WEB") is True
    assert pattern_matches("1080p", "Some.Show.720p.WEB") is False


def test_glob_pattern_is_strict_not_substring():
    assert pattern_matches("*.nfo", "notes.nfo") is True
    assert pattern_matches("*.nfo", "notes.nfo.bak") is False  # glob is anchored, not substring


def test_matching_is_case_insensitive_for_both_dispatch_paths():
    assert pattern_matches("SAMPLE", "a.sample.file") is True
    assert pattern_matches("*.NFO", "notes.nfo") is True


def test_glob_dispatch_triggers_on_question_mark_and_bracket_too_not_only_star():
    # "a?c" is not a literal substring of "abc" -- if this matched, dispatch would be
    # treating it as plain substring instead of glob. fnmatch anchors the whole name (same
    # as the "*.nfo" example in DESIGN.md §4.7), so the bracket case needs its own wildcards.
    assert pattern_matches("a?c", "abc") is True
    assert pattern_matches("*s0[1-3]e01*", "show.s02e01.mkv") is True
    assert pattern_matches("*s0[1-3]e01*", "show.s04e01.mkv") is False


# --- select/skip semantics, skip beats select (§4.7) -------------------------------------


def test_no_select_patterns_matches_everything_by_default():
    compiled = CompiledPatterns.compile([])
    assert compiled.item_matches("Anything.At.All") is True


def test_no_select_patterns_matches_nothing_under_patterns_only():
    compiled = CompiledPatterns.compile([], patterns_only=True)
    assert compiled.item_matches("Anything.At.All") is False


def test_select_pattern_restricts_to_matches():
    compiled = CompiledPatterns.compile([Pattern(kind="select", expr="*.mkv")])
    assert compiled.item_matches("Movie.2024.mkv") is True
    assert compiled.item_matches("Movie.2024.avi") is False


def test_skip_beats_select():
    compiled = CompiledPatterns.compile(
        [Pattern(kind="select", expr="*"), Pattern(kind="skip", expr="*SAMPLE*")]
    )
    assert compiled.item_matches("Some.Release.SAMPLE") is False
    assert compiled.item_matches("Some.Release") is True


def test_disabled_pattern_is_never_compiled_in():
    compiled = CompiledPatterns.compile([Pattern(kind="skip", expr="*", enabled=False)])
    assert compiled.item_matches("anything") is True


# --- file_exclude also applies to loose top-level files (§4.7) --------------------------


def test_file_exclude_suppresses_a_loose_top_level_file_item():
    compiled = CompiledPatterns.compile([Pattern(kind="file_exclude", expr="*.nfo")])
    assert compiled.item_matches("notes.nfo", is_file=True) is False
    # A directory named "notes.nfo" (contrived, but the point is is_file gates this) is
    # unaffected -- file_exclude only applies to file items, per §4.7.
    assert compiled.item_matches("notes.nfo", is_file=False) is True


def test_select_pattern_does_not_match_a_directory_containing_a_matching_file():
    # DESIGN.md §4.7: item patterns see item names, never contents. "*.mkv" as a select must
    # not match "Movie.2024/" just because it holds an mkv -- it's evaluated on the
    # directory's own name only, which the caller (auto-queue) is responsible for passing.
    compiled = CompiledPatterns.compile([Pattern(kind="select", expr="*.mkv")])
    assert compiled.item_matches("Movie.2024") is False


# --- exclude_globs() for lftp --exclude-glob ---------------------------------------------


def test_exclude_globs_passes_glob_patterns_through_unchanged():
    compiled = CompiledPatterns.compile([Pattern(kind="file_exclude", expr="*.nfo")])
    assert compiled.exclude_globs() == ("*.nfo",)


def test_exclude_globs_wraps_plain_substring_patterns_for_lftp():
    # lftp itself has no substring-match mode, so a plain "sample" file_exclude must become
    # a glob lftp actually understands.
    compiled = CompiledPatterns.compile([Pattern(kind="file_exclude", expr="sample")])
    assert compiled.exclude_globs() == ("*sample*",)


# --- build_counts_predicate() -- the reconciler-facing half of the same evaluator --------


def test_counts_predicate_excludes_a_matching_file_at_any_depth():
    compiled = CompiledPatterns.compile([Pattern(kind="file_exclude", expr="*.nfo")])
    predicate = build_counts_predicate(compiled)

    nfo = RemoteEntry(rel_path="Release/notes.nfo", is_dir=False, size=5, mtime=1.0)
    mkv = RemoteEntry(rel_path="Release/movie.mkv", is_dir=False, size=1000, mtime=1.0)
    top_level_nfo = RemoteEntry(rel_path="notes.nfo", is_dir=False, size=5, mtime=1.0)

    assert predicate("Release/notes.nfo", nfo) is False
    assert predicate("Release/movie.mkv", mkv) is True
    assert predicate("notes.nfo", top_level_nfo) is False  # top-level files are not exempt
