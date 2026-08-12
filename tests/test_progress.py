"""core/progress.py (DESIGN.md §4.4) — EMA speed/ETA over the active set, no lftp stdout."""

from __future__ import annotations

from lftpweb.core.progress import ActiveJob, ProgressSampler


def test_first_sample_has_zero_speed_no_history_to_derive_a_rate_from(tmp_path):
    f = tmp_path / "movie.mkv"
    f.write_bytes(b"x" * 1000)
    sampler = ProgressSampler()
    result = sampler.sample([ActiveJob(job_id=1, kind="pget", local_root=str(f), bytes_total=10_000)], now=0.0)
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
    (root / "a.mkv.lftp-pget-status").write_text("size=1000\n0.pos=0\n0.limit=1000\n")  # nothing written yet
    sampler = ProgressSampler()
    result = sampler.sample([ActiveJob(job_id=1, kind="mirror", local_root=str(root), bytes_total=1000)], now=0.0)
    assert result[1].bytes_done == 0


def test_dropping_a_finished_job_clears_its_speed_history():
    sampler = ProgressSampler()
    sampler.sample([ActiveJob(job_id=1, kind="pget", local_root="/nonexistent", bytes_total=None)], now=0.0)
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
    jobs = [ActiveJob(job_id=1, kind="pget", local_root=str(tmp_path / "not-started.mkv"), bytes_total=5000)]
    result = sampler.sample(jobs, now=0.0)
    assert result[1].bytes_done == 0
    assert result[1].speed_bps == 0.0
