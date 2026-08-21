"""Unit tests for `core/mount_sentinel.py` -- the mount gate and the local-absence grace
period, required starting phase 4 rather than deferred with `sync` (DESIGN.md §7.3,
docs/decisions.md).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from lftpweb.core.mount_sentinel import (
    COMPLETE_STATES,
    DEFAULT_GRACE_S,
    SENTINEL_NAME,
    check,
    resolve_absence,
    resolve_vanished,
    write_if_needed,
)


# --- check() / write_if_needed() ---------------------------------------------------------


def test_check_false_when_root_missing(tmp_path):
    assert check(str(tmp_path / "does-not-exist")) is False


def test_check_false_on_the_nastiest_case_empty_readable_root_no_sentinel(tmp_path):
    # DESIGN.md §14 names this one explicitly: root exists, is readable, is empty, has no
    # sentinel. Indistinguishable from an unmounted share by content alone.
    root = tmp_path / "local"
    root.mkdir()
    assert check(str(root)) is False


def test_write_then_check_round_trips(tmp_path):
    root = tmp_path / "local"
    root.mkdir()
    write_if_needed(str(root))
    assert (root / SENTINEL_NAME).is_file()
    assert check(str(root)) is True


def test_write_if_needed_does_not_create_the_root_itself(tmp_path):
    # A not-yet-mounted root must never earn trust just because we tried to write into it.
    missing_root = tmp_path / "not-mounted-yet"
    write_if_needed(str(missing_root))
    assert not missing_root.exists()


def test_write_if_needed_is_idempotent(tmp_path):
    root = tmp_path / "local"
    root.mkdir()
    write_if_needed(str(root))
    first_contents = (root / SENTINEL_NAME).read_text()
    write_if_needed(str(root))  # second call must not overwrite/touch it again
    assert (root / SENTINEL_NAME).read_text() == first_contents


# --- resolve_absence(): the grace period + mount gate, as a pure function ----------------


def test_never_downloaded_item_is_left_to_the_structural_state():
    assert (
        resolve_absence(
            prev_state="REMOTE_ONLY",
            prev_first_missing_at=None,
            structural_state="REMOTE_ONLY",
            mount_ok=True,
            now=datetime.now(UTC),
        )
        is None
    )


def test_first_observed_absence_starts_the_clock_but_keeps_downloaded():
    state, first_missing_at = resolve_absence(
        prev_state="DOWNLOADED",
        prev_first_missing_at=None,
        structural_state="REMOTE_ONLY",
        mount_ok=True,
        now=datetime.now(UTC),
    )
    assert state == "DOWNLOADED"
    assert first_missing_at is not None


def test_absence_within_the_grace_window_stays_downloaded():
    now = datetime.now(UTC)
    started = (now - timedelta(seconds=DEFAULT_GRACE_S / 2)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    state, first_missing_at = resolve_absence(
        prev_state="DOWNLOADED",
        prev_first_missing_at=started,
        structural_state="REMOTE_ONLY",
        mount_ok=True,
        now=now,
    )
    assert state == "DOWNLOADED"
    assert first_missing_at == started


def test_absence_past_the_grace_window_becomes_removed_local():
    now = datetime.now(UTC)
    started = (now - timedelta(seconds=DEFAULT_GRACE_S + 1)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    state, first_missing_at = resolve_absence(
        prev_state="DOWNLOADED",
        prev_first_missing_at=started,
        structural_state="REMOTE_ONLY",
        mount_ok=True,
        now=now,
    )
    assert state == "REMOVED_LOCAL"
    assert first_missing_at == started


def test_reappearance_clears_by_returning_none_for_the_caller_to_drop_first_missing_at():
    # Once local presence is back, reconcile() itself reports PARTIAL/DOWNLOADED rather than
    # REMOTE_ONLY -- resolve_absence has nothing to override, so the caller (core/engine.py)
    # persists the fresh structural state and a fresh (cleared) first_missing_at.
    assert (
        resolve_absence(
            prev_state="REMOVED_LOCAL",
            prev_first_missing_at="2026-01-01T00:00:00.000000Z",
            structural_state="DOWNLOADED",
            mount_ok=True,
            now=datetime.now(UTC),
        )
        is None
    )


def test_mount_gate_refuses_to_start_the_clock_when_mount_is_down():
    state, first_missing_at = resolve_absence(
        prev_state="DOWNLOADED",
        prev_first_missing_at=None,
        structural_state="REMOTE_ONLY",
        mount_ok=False,
        now=datetime.now(UTC),
    )
    assert state == "DOWNLOADED"
    assert first_missing_at is None  # never started


def test_mount_gate_freezes_an_already_running_clock_too():
    now = datetime.now(UTC)
    started = (now - timedelta(seconds=DEFAULT_GRACE_S + 1)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    state, first_missing_at = resolve_absence(
        prev_state="DOWNLOADED",
        prev_first_missing_at=started,
        structural_state="REMOTE_ONLY",
        mount_ok=False,  # mount just dropped -- even though grace has technically elapsed
        now=now,
    )
    assert state == "DOWNLOADED"  # never crosses into REMOVED_LOCAL while the mount is down
    assert first_missing_at == started


def test_removed_local_is_sticky_regardless_of_mount_state():
    # Once correctly classified while the mount was healthy, a later mount drop must not
    # undo it -- that's not the mount gate's job (it guards *new* transitions).
    state, first_missing_at = resolve_absence(
        prev_state="REMOVED_LOCAL",
        prev_first_missing_at="2026-01-01T00:00:00.000000Z",
        structural_state="REMOTE_ONLY",
        mount_ok=False,
        now=datetime.now(UTC),
    )
    assert state == "REMOVED_LOCAL"
    assert first_missing_at == "2026-01-01T00:00:00.000000Z"


# --- ...and the same for every state core/postprocess.py owns (DESIGN.md §3.2, §6) --------
#
# Each of the six is only ever written to an item that had already reached DOWNLOADED, so
# "locally absent now" means exactly what it means for a plain DOWNLOADED item. Leaving them
# out of the sticky set -- as this module did until the post-processing states were made to
# survive a rescan at all -- would persist a fresh REMOTE_ONLY for them instead, and
# auto-queue would re-download every release an *arr importer had just moved out.


@pytest.mark.parametrize(
    "prev_state",
    ["DOWNLOADED", "VERIFYING", "VERIFIED", "CORRUPT", "EXTRACTING", "EXTRACTED", "EXTRACT_FAILED"],
)
def test_every_complete_local_state_starts_the_clock_and_holds_itself(prev_state):
    state, first_missing_at = resolve_absence(
        prev_state=prev_state,
        prev_first_missing_at=None,
        structural_state="REMOTE_ONLY",
        mount_ok=True,
        now=datetime.now(UTC),
    )
    # The item keeps reading CORRUPT/EXTRACT_FAILED/... during the window rather than being
    # downgraded to DOWNLOADED first: the failure a user needs to see must not be lost on the
    # way to REMOVED_LOCAL.
    assert state == prev_state
    assert first_missing_at is not None


@pytest.mark.parametrize(
    "prev_state",
    ["DOWNLOADED", "VERIFYING", "VERIFIED", "CORRUPT", "EXTRACTING", "EXTRACTED", "EXTRACT_FAILED"],
)
def test_every_complete_local_state_reaches_removed_local_after_the_grace_period(prev_state):
    now = datetime.now(UTC)
    started = (now - timedelta(seconds=DEFAULT_GRACE_S + 1)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    state, first_missing_at = resolve_absence(
        prev_state=prev_state,
        prev_first_missing_at=started,
        structural_state="REMOTE_ONLY",
        mount_ok=True,
        now=now,
    )
    assert state == "REMOVED_LOCAL"
    assert first_missing_at == started


@pytest.mark.parametrize("prev_state", ["VERIFIED", "CORRUPT", "EXTRACTED", "EXTRACT_FAILED"])
def test_the_mount_gate_applies_to_post_processing_states_too(prev_state):
    state, first_missing_at = resolve_absence(
        prev_state=prev_state,
        prev_first_missing_at=None,
        structural_state="REMOTE_ONLY",
        mount_ok=False,
        now=datetime.now(UTC),
    )
    assert state == prev_state
    assert first_missing_at is None  # a dropped mount never starts the clock


@pytest.mark.parametrize("prev_state", ["VERIFIED", "CORRUPT", "EXTRACTED", "EXTRACT_FAILED"])
@pytest.mark.parametrize("structural_state", ["DOWNLOADED", "PARTIAL"])
def test_presence_is_not_this_functions_decision(prev_state, structural_state):
    # Content still present is not absence: whether an outcome outranks a fresh DOWNLOADED is
    # decided by core/postprocess.py.outcome_survives_rescan, applied by core/engine.py before
    # this function is consulted. Returning None here keeps the two halves from overlapping.
    #
    # `PARTIAL` is in the parametrization on purpose even though 2026-08-19 gave it a branch of
    # its own: without `prev_remote_size`/`remote_size` there is no evidence of a *shrink*, and
    # "no information" must never be read as one -- so this call, and every caller that predates
    # those two arguments, behaves exactly as it always did.
    assert (
        resolve_absence(
            prev_state=prev_state,
            prev_first_missing_at=None,
            structural_state=structural_state,
            mount_ok=True,
            now=datetime.now(UTC),
        )
        is None
    )


# --- resolve_absence(): the *partial* half of the same grace period (2026-08-19) -----------
#
# `prompts/done/2026-08-19-autoqueue-requeues-imported-item.md`, production v0.2.6: an importer
# takes a finished release apart one file at a time, so the reading between "complete" and
# "gone" is PARTIAL -- an auto-queue-eligible state that had no grace-period protection at all,
# and re-queued a release whose seedbox source was about to be deleted on confirmed import.
#
# Every test below pins the *narrowness* of the key as hard as the behaviour, because PARTIAL
# being re-queueable is how a genuinely interrupted transfer resumes.


def _shrink(**overrides):
    kwargs = {
        "prev_state": "DOWNLOADED",
        "prev_first_missing_at": None,
        "structural_state": "PARTIAL",
        "mount_ok": True,
        "now": datetime.now(UTC),
        "prev_remote_size": 1_000,
        "remote_size": 1_000,
    }
    kwargs.update(overrides)
    return resolve_absence(**kwargs)


@pytest.mark.parametrize("prev_state", sorted(COMPLETE_STATES))
def test_a_shrink_from_any_complete_state_starts_the_clock_and_holds_that_state(prev_state):
    state, first_missing_at = _shrink(prev_state=prev_state)
    assert state == prev_state
    assert first_missing_at is not None


@pytest.mark.parametrize("prev_state", sorted(COMPLETE_STATES))
def test_a_shrink_past_the_grace_window_releases_the_fresh_partial(prev_state):
    now = datetime.now(UTC)
    stale = (now - timedelta(seconds=DEFAULT_GRACE_S + 1)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    # Deliberately None (= "trust PARTIAL"), never REMOVED_LOCAL: content is still on disk, and
    # a damaged local copy must stay re-fetchable. See resolve_absence's own docstring.
    assert _shrink(prev_state=prev_state, prev_first_missing_at=stale, now=now) is None


def test_a_shrink_within_the_grace_window_does_not_restart_the_clock():
    now = datetime.now(UTC)
    started = (now - timedelta(seconds=60)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    assert _shrink(prev_first_missing_at=started, now=now) == ("DOWNLOADED", started)


@pytest.mark.parametrize(
    "prev_state", ["PARTIAL", "REMOTE_ONLY", "STOPPED", "FAILED", "REMOVED_LOCAL", "LOCAL_ONLY"]
)
def test_a_partial_from_a_state_that_never_asserted_completeness_is_untouched(prev_state):
    """The guard. A transfer that stopped short is `PARTIAL`/`STOPPED`/`FAILED` beforehand,
    never one of `COMPLETE_STATES` -- so the branch cannot reach it, and re-queue still works.
    """
    assert _shrink(prev_state=prev_state) is None


@pytest.mark.parametrize(("prev_remote_size", "remote_size"), [(500, 1_000), (1_000, 500)])
def test_a_changed_remote_total_is_not_a_shrink(prev_remote_size, remote_size):
    """§3.2 rule 4: remote size is a moving target. A previously-complete item whose remote
    **grew** genuinely has more to fetch and must read PARTIAL at once, with no hold.
    """
    assert _shrink(prev_remote_size=prev_remote_size, remote_size=remote_size) is None


@pytest.mark.parametrize(("prev_remote_size", "remote_size"), [(None, 1_000), (1_000, None)])
def test_an_unknown_remote_total_is_never_inferred_to_be_a_shrink(prev_remote_size, remote_size):
    assert _shrink(prev_remote_size=prev_remote_size, remote_size=remote_size) is None


def test_the_mount_gate_refuses_to_start_the_shrink_clock_too():
    state, first_missing_at = _shrink(mount_ok=False)
    assert state == "DOWNLOADED"
    assert first_missing_at is None


def test_the_mount_gate_freezes_a_running_shrink_clock_rather_than_letting_it_expire():
    now = datetime.now(UTC)
    stale = (now - timedelta(seconds=DEFAULT_GRACE_S + 1)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    assert _shrink(mount_ok=False, prev_first_missing_at=stale, now=now) == ("DOWNLOADED", stale)


# --- resolve_vanished(): the "no opinion" fallback for a path in neither tree at all --------
#
# 2026-08-13 (prompts/2026-08-13-delete-state-truthfulness.md, defect 3): `resolve_absence`
# returns None for a `prev_state` outside `_STICKY_PREV_STATES` -- correct for that function's
# own job, but `core/engine.py._persist`'s vanished-from-both-trees sweep has nothing else to
# fall back to, and without one, such a row is simply never written again. Deliberately narrow:
# only `PARTIAL`/`LOCAL_ONLY` (content that was actually, concretely here) get a resting state;
# `REMOTE_ONLY`/`EXCLUDED` (nothing was ever here, or never going to be, on purpose) keep the
# pre-existing "silently drops from the published tree" behavior -- see the module-level
# comment on `_VANISHED_FALLBACK_PREV_STATES` for why widening further would be a regression,
# not a fix (`tests/test_ws_deltas.py`'s scan-delta tests depend on the `REMOTE_ONLY` case).


@pytest.mark.parametrize("prev_state", ["PARTIAL", "LOCAL_ONLY"])
def test_resolve_vanished_rests_a_no_opinion_prev_state_at_removed_both(prev_state):
    assert resolve_vanished(prev_state) == "REMOVED_BOTH"


@pytest.mark.parametrize(
    "prev_state", ["REMOTE_ONLY", "EXCLUDED", "QUEUED", "STOPPED", "REMOVED_BOTH"]
)
def test_resolve_vanished_leaves_every_other_prev_state_alone(prev_state):
    # REMOTE_ONLY/EXCLUDED never asserted concrete content; QUEUED/STOPPED would never reach
    # this function in practice (protected rows never enter the vanished sweep) but are checked
    # anyway as documentation; REMOVED_BOTH is the idempotent already-resting-here case -- the
    # vanished sweep runs every scan a row stays gone, and a REMOVED_BOTH this function itself
    # produced last pass must not be treated as a fresh "no opinion" case on the next one.
    assert resolve_vanished(prev_state) is None


@pytest.mark.parametrize(
    "prev_state",
    ["DOWNLOADED", "VERIFIED", "EXTRACTED", "REMOVED_LOCAL"],
)
def test_resolve_vanished_is_never_consulted_for_a_sticky_prev_state(prev_state):
    # Not a claim this function makes itself (its caller only ever reaches it when
    # resolve_absence already returned None) -- asserted here anyway as documentation: every
    # sticky state has its own opinion and would never legitimately reach resolve_vanished.
    assert (
        resolve_absence(
            prev_state=prev_state,
            prev_first_missing_at=None,
            structural_state="REMOTE_ONLY",
            mount_ok=True,
            now=datetime.now(UTC),
        )
        is not None
    )
