"""core/progress.py (DESIGN.md §4.4) — EMA speed/ETA over the active set, no lftp stdout."""

from __future__ import annotations

from lftpweb.core.progress import ActiveJob, ProgressSampler, child_speed_bps, ema_step


def test_first_sample_has_zero_speed_no_history_to_derive_a_rate_from(tmp_path):
    f = tmp_path / "movie.mkv"
    f.write_bytes(b"x" * 1000)
    sampler = ProgressSampler()
    result = sampler.sample(
        [ActiveJob(job_id=1, kind="pget", local_root=str(f), bytes_total=10_000)], now=0.0
    )
    assert result[1].bytes_done == 1000
    assert result[1].speed_bps == 0.0
    assert result[1].eta_s is None  # speed is 0 -> no ETA


def test_speed_and_eta_after_a_second_tick(tmp_path):
    f = tmp_path / "movie.mkv"
    f.write_bytes(b"x" * 1000)
    sampler = ProgressSampler(alpha=1.0)  # alpha=1 -> speed is exactly the instantaneous rate
    jobs = [ActiveJob(job_id=1, kind="pget", local_root=str(f), bytes_total=10_000)]
    sampler.sample(jobs, now=0.0)
    f.write_bytes(b"x" * 3000)  # +2000 bytes over 1 second
    result = sampler.sample(jobs, now=1.0)
    assert result[1].bytes_done == 3000
    assert result[1].speed_bps == 2000.0
    assert result[1].eta_s == (10_000 - 3000) / 2000.0


def test_ema_smooths_across_ticks_rather_than_snapping_to_instantaneous(tmp_path):
    f = tmp_path / "movie.mkv"
    f.write_bytes(b"x" * 0)
    sampler = ProgressSampler(alpha=0.3)
    jobs = [ActiveJob(job_id=1, kind="pget", local_root=str(f), bytes_total=None)]
    sampler.sample(jobs, now=0.0)
    f.write_bytes(b"x" * 1000)  # 1000 B/s
    r1 = sampler.sample(jobs, now=1.0)
    assert r1[1].speed_bps == 300.0  # 0.3*1000 + 0.7*1000 (first speed seeds with instantaneous)
    f.write_bytes(b"x" * 1000)  # no further progress this tick -> instantaneous 0
    r2 = sampler.sample(jobs, now=2.0)
    assert r2[1].speed_bps == 0.3 * 0 + 0.7 * 300.0


def test_directory_job_sums_effective_size_over_its_subtree(tmp_path):
    root = tmp_path / "Release"
    root.mkdir()
    (root / "a.mkv").write_bytes(b"a" * 100)
    (root / "b.nfo").write_bytes(b"b" * 10)
    sampler = ProgressSampler()
    jobs = [ActiveJob(job_id=1, kind="mirror", local_root=str(root), bytes_total=200)]
    result = sampler.sample(jobs, now=0.0)
    assert result[1].bytes_done == 110


def test_directory_job_honors_pget_status_sidecars_per_file(tmp_path):
    root = tmp_path / "Release"
    root.mkdir()
    with open(root / "a.mkv", "wb") as f:
        f.truncate(1000)
    (root / "a.mkv.lftp-pget-status").write_text(
        "size=1000\n0.pos=0\n0.limit=1000\n"
    )  # nothing written yet
    sampler = ProgressSampler()
    result = sampler.sample(
        [ActiveJob(job_id=1, kind="mirror", local_root=str(root), bytes_total=1000)], now=0.0
    )
    assert result[1].bytes_done == 0


def test_dropping_a_finished_job_clears_its_speed_history():
    sampler = ProgressSampler()
    sampler.sample(
        [ActiveJob(job_id=1, kind="pget", local_root="/nonexistent", bytes_total=None)], now=0.0
    )
    assert 1 in sampler._prev_bytes
    sampler.drop(1)
    assert 1 not in sampler._prev_bytes
    assert 1 not in sampler._speed


def test_jobs_no_longer_active_are_pruned_automatically_on_next_sample(tmp_path):
    f = tmp_path / "a.mkv"
    f.write_bytes(b"x" * 10)
    sampler = ProgressSampler()
    sampler.sample([ActiveJob(job_id=1, kind="pget", local_root=str(f), bytes_total=None)], now=0.0)
    sampler.sample([], now=1.0)  # job 1 no longer in the active set
    assert 1 not in sampler._prev_bytes


def test_missing_local_root_reads_as_zero_bytes_done_not_an_error(tmp_path):
    sampler = ProgressSampler()
    jobs = [
        ActiveJob(
            job_id=1, kind="pget", local_root=str(tmp_path / "not-started.mkv"), bytes_total=5000
        )
    ]
    result = sampler.sample(jobs, now=0.0)
    assert result[1].bytes_done == 0
    assert result[1].speed_bps == 0.0


# --- ema_step (extracted so core/queue.py's per-child rate can reuse the exact formula) -------


def test_ema_step_seeds_at_instantaneous_with_no_prior_speed():
    assert ema_step(1000.0, None, alpha=0.3) == 1000.0


def test_ema_step_blends_instantaneous_and_previous_speed():
    assert ema_step(0.0, 300.0, alpha=0.3) == 0.3 * 0.0 + 0.7 * 300.0


def test_ema_step_matches_progress_samplers_own_math():
    """The refactor in `ProgressSampler.sample` (extracting `ema_step`) must not have changed
    its output -- mirrors `test_ema_smooths_across_ticks_rather_than_snapping_to_instantaneous`
    above's real two-tick sequence through `ProgressSampler.sample`, recomputed here directly
    through `ema_step` alone. Tick 1 there is `ProgressSampler`'s own "first sample, no history"
    special case -- it sets speed to `0.0` *without* calling `ema_step` at all (there is nothing
    to blend yet) -- so this starts from that same `0.0` rather than re-deriving it.
    """
    speed_after_tick1 = 0.0  # ProgressSampler.sample's own first-sample convention
    speed_after_tick2 = ema_step(1000.0, speed_after_tick1, alpha=0.3)
    assert speed_after_tick2 == 300.0
    speed_after_tick3 = ema_step(0.0, speed_after_tick2, alpha=0.3)
    assert speed_after_tick3 == 0.3 * 0 + 0.7 * 300.0


# --- child_speed_bps (this task, 2026-08-14: per-file speed inside a mirror) ------------------
#
# Pure-function coverage for the rate `core/queue.py._publish_child_progress` derives per
# child, independent of the database/queue machinery -- a normal delta, a zero delta, a
# *negative* delta (a file replaced or truncated mid-transfer must not produce a negative
# rate), and a zero/sub-second elapsed (two throttled ticks close enough together that the
# real clock didn't visibly advance -- must not divide by zero or go negative).


def test_child_speed_bps_normal_delta():
    # 500 bytes over 1 real second, no prior speed -> seeds at the instantaneous rate.
    assert child_speed_bps(500, 1.0, None, alpha=0.3) == 500.0


def test_child_speed_bps_zero_delta_reads_as_zero_not_negative_or_nan():
    assert child_speed_bps(0, 1.0, 200.0, alpha=0.3) == 0.3 * 0.0 + 0.7 * 200.0


def test_child_speed_bps_negative_delta_never_produces_a_negative_rate():
    # A file replaced or truncated mid-transfer can report a smaller size than the last
    # sample -- clamped to "no progress this tick" (delta 0), same as `ProgressSampler.sample`'s
    # own `max(bytes_done - prev_bytes, 0)` at the job level, so the result is never negative
    # regardless of how negative the raw delta or how large `prev_speed` isn't.
    result = child_speed_bps(-500, 1.0, 200.0, alpha=0.3)
    assert result >= 0
    assert result == ema_step(0.0, 200.0, alpha=0.3)  # negative delta clamped to 0 internally


def test_child_speed_bps_large_negative_delta_with_no_prior_speed_is_still_non_negative():
    # No history to seed from either -- clamped delta (0) seeds the EMA at 0, not at the raw
    # negative instantaneous rate.
    assert child_speed_bps(-1_000_000, 1.0, None, alpha=0.3) == 0.0


def test_child_speed_bps_zero_elapsed_returns_zero_not_a_division_error():
    assert child_speed_bps(500, 0.0, 300.0, alpha=0.3) == 0.0


def test_child_speed_bps_sub_zero_elapsed_returns_zero():
    # A monotonic clock never goes backwards in practice, but a defensive negative elapsed
    # (or a caller mistake) must not raise or produce a nonsensical rate.
    assert child_speed_bps(500, -1.0, 300.0, alpha=0.3) == 0.0


def test_child_speed_bps_smooths_with_ema_not_a_raw_instantaneous_snap():
    first = child_speed_bps(1000, 1.0, None, alpha=0.3)
    assert first == 1000.0
    second = child_speed_bps(0, 1.0, first, alpha=0.3)
    assert second == 0.3 * 0.0 + 0.7 * 1000.0
    assert second != 0.0  # a raw per-tick delta would have snapped straight to zero
