"""spec §9.2 -- the *arr/download-client Preflight merge (`api/jobs.py._merge_preflight_rows`,
`_merge_arr_and_client_rows`, `_merge_client_field_into_arr`, 2026-08-23, this task). Pure unit
tests against `PreflightRow` values directly -- no DB, no server, no poller: the merge itself is
the thing under test, and every one of its inputs is exactly what a real `ArrSyncScheduler`/
`ClientSyncScheduler` pass would have handed it.
"""

from __future__ import annotations

from lftpweb.api.jobs import (
    _merge_arr_and_client_rows,
    _merge_client_field_into_arr,
    _merge_preflight_rows,
)
from lftpweb.core.preflight import PreflightRow


def _row(**overrides) -> PreflightRow:
    defaults = dict(
        source="arr",
        queue_id=1,
        queue_name="TV",
        queue_short_name="tv",
        title="Show.S01E05.1080p-GRP",
        status_label="downloading",
        source_label="Sonarr",
        source_kind="sonarr",
        size_bytes=None,
        size_remaining_bytes=None,
        remaining_s=None,
        download_client=None,
        wait_scans=None,
        wait_since=None,
        download_id=None,
    )
    defaults.update(overrides)
    return PreflightRow(**defaults)


def _client_row(**overrides) -> PreflightRow:
    defaults = dict(
        source="client",
        queue_id=1,
        queue_name="TV",
        queue_short_name="tv",
        title="Show.S01E05.1080p-GRP",
        status_label="Downloading",
        source_label="SABnzbd",
        source_kind="sabnzbd",
        size_bytes=None,
        size_remaining_bytes=None,
        remaining_s=None,
        download_client=None,
        wait_scans=None,
        wait_since=None,
        download_id="abc123",
    )
    defaults.update(overrides)
    return PreflightRow(**defaults)


# --- The core rule: client wins per field, only where it actually reported ----------------------


def test_client_field_wins_over_arr_when_both_report():
    """Not "if newer" -- the client always wins on a field it populated, full stop (spec §9.2's
    own "always, not if newer" -- there is no timestamp to compare)."""
    arr_row = _row(download_id="abc123", remaining_s=120.0, size_bytes=1000)
    client_row = _client_row(remaining_s=30.0, size_bytes=1000)
    merged = _merge_client_field_into_arr(arr_row, client_row)
    assert merged.remaining_s == 30.0


def test_stale_arr_field_not_overwritten_by_absent_client_field():
    """The failure this task's own handoff prompt calls out by name: a client that reports no
    ETA this pass must not blank the *arr's own `timeleft` -- silence is not "the client says
    zero," it's "the client said nothing about this field" (spec §4.2 outranking §9.2)."""
    arr_row = _row(download_id="abc123", remaining_s=120.0)
    client_row = _client_row(remaining_s=None)  # the client didn't report an ETA this pass
    merged = _merge_client_field_into_arr(arr_row, client_row)
    assert merged.remaining_s == 120.0  # the *arr's own stale-but-only reading survives


def test_precedence_is_per_field_not_per_record():
    """A client row that populates one field and not another must not blank the *arr's value for
    the field it left `None`, *while still* winning on the field it did populate -- proving the
    merge is genuinely per-field, not "pick one row's data wholesale."""
    arr_row = _row(download_id="abc123", size_bytes=5_000_000, remaining_s=600.0)
    client_row = _client_row(size_bytes=6_000_000, remaining_s=None)
    merged = _merge_client_field_into_arr(arr_row, client_row)
    assert merged.size_bytes == 6_000_000  # client wins, it reported
    assert merged.remaining_s == 600.0  # arr's stands, client was silent on this one


def test_client_size_remaining_bytes_wins_when_reported():
    arr_row = _row(download_id="abc123", size_remaining_bytes=999)
    client_row = _client_row(size_remaining_bytes=111)
    merged = _merge_client_field_into_arr(arr_row, client_row)
    assert merged.size_remaining_bytes == 111


def test_client_status_label_always_wins_when_present():
    """`status_label` is mandatory on every `Transfer` (spec §2.2), so a client row always has
    one -- it wins unconditionally, the client's own word displacing the *arr's relayed one."""
    arr_row = _row(download_id="abc123", status_label="queued")
    client_row = _client_row(status_label="Downloading")
    merged = _merge_client_field_into_arr(arr_row, client_row)
    assert merged.status_label == "Downloading"


def test_client_display_identity_and_download_client_tooltip_field_preserved():
    arr_row = _row(download_id="abc123", download_client="SABnzbd (via Sonarr)")
    client_row = _client_row()
    merged = _merge_client_field_into_arr(arr_row, client_row)
    assert merged.source == "client"
    assert merged.source_label == "SABnzbd"
    assert merged.source_kind == "sabnzbd"
    # The *arr's own "which client is fetching this" tooltip fact is preserved -- the client
    # source itself never populates `download_client` (it *is* the client).
    assert merged.download_client == "SABnzbd (via Sonarr)"


# --- Dedupe by `download_id` -- exact identity, no heuristics (spec §7.1, §9.2) ------------------


def test_dedupe_by_download_id_produces_exactly_one_row():
    arr_row = _row(download_id="abc123")
    client_row = _client_row(download_id="abc123")
    merged = _merge_arr_and_client_rows([arr_row], [client_row])
    assert len(merged) == 1
    assert merged[0].source == "client"


def test_rows_without_a_shared_download_id_never_merge():
    arr_row = _row(download_id="abc123", title="Show A")
    client_row = _client_row(download_id="xyz789", title="Show B")
    merged = _merge_arr_and_client_rows([arr_row], [client_row])
    assert len(merged) == 2


def test_rows_with_no_download_id_at_all_pass_through_standalone():
    """Neither source is guaranteed to carry a `download_id` (spec: `None` when a source has
    nothing of the kind to report) -- a row missing it must never merge with anything, on either
    side, and must never be silently dropped either."""
    arr_row = _row(download_id=None)
    client_row = _client_row(download_id=None)
    merged = _merge_arr_and_client_rows([arr_row], [client_row])
    assert len(merged) == 2


def test_client_only_row_with_no_matching_arr_row_shown_standalone():
    """No *arr configured, or the *arr simply hasn't grabbed it under a downloadId this pass yet
    -- the client's own row still surfaces on its own, unmerged."""
    client_row = _client_row(download_id="abc123")
    merged = _merge_arr_and_client_rows([], [client_row])
    assert merged == [client_row]


# --- The full three-way merge (arr/client, then settle precedence) ------------------------------


def test_settle_still_wins_over_a_merged_arr_client_row():
    arr_row = _row(download_id="abc123", queue_id=1, title="Show.S01E05.1080p-GRP")
    client_row = _client_row(download_id="abc123", queue_id=1, title="Show.S01E05.1080p-GRP")
    settle_row = _row(
        source="settle",
        download_id=None,
        queue_id=1,
        title="Show.S01E05.1080p-GRP",
        status_label="Settling",
    )
    merged = _merge_preflight_rows([arr_row], [client_row], [settle_row])
    assert len(merged) == 1
    assert merged[0].source == "settle"


def test_three_way_merge_orders_alphabetically_by_title():
    arr_row = _row(download_id="1", title="Zeta")
    client_row = _client_row(download_id="2", title="Alpha")
    settle_row = _row(source="settle", download_id=None, title="Mid", queue_id=2)
    merged = _merge_preflight_rows([arr_row], [client_row], [settle_row])
    assert [r.title for r in merged] == ["Alpha", "Mid", "Zeta"]
