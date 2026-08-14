"""`core/queue.py.compute_retry_backoff` -- the retry delay arithmetic.

Exists because `TransferSettings.retry_backoff_base_s` was a dead setting from phase 3a until
2026-08-14: it loaded, saved, and round-tripped through `PUT /api/settings/transfer` and the
Settings -> Transfer form, while `TransferQueue._reap_one` computed the actual delay from the
`DEFAULT_RETRY_BACKOFF_BASE_S` module constant and never read the saved value. Changing the
field did nothing at all.

No test covered it, which is precisely why it survived nine phases plus two live-testing
sessions. These assert the two properties that were silently broken: the delay is driven by the
*passed* base rather than the constant, and the cap still applies.
"""

from lftpweb.core.queue import (
    DEFAULT_RETRY_BACKOFF_BASE_S,
    DEFAULT_RETRY_BACKOFF_MAX_S,
    compute_retry_backoff,
)


def test_first_attempt_waits_exactly_the_base():
    assert compute_retry_backoff(30.0, 1) == 30.0
    assert compute_retry_backoff(5.0, 1) == 5.0


def test_delay_doubles_per_attempt():
    assert compute_retry_backoff(30.0, 2) == 60.0
    assert compute_retry_backoff(30.0, 3) == 120.0


def test_a_custom_base_actually_changes_the_delay():
    """The regression this file exists for. A base other than the module default must produce a
    different delay -- when `_reap_one` read the constant, every one of these returned the
    30s-derived value regardless of what the user had saved.
    """
    assert compute_retry_backoff(5.0, 1) != compute_retry_backoff(DEFAULT_RETRY_BACKOFF_BASE_S, 1)
    assert compute_retry_backoff(5.0, 3) == 20.0
    assert compute_retry_backoff(120.0, 2) == 240.0


def test_clamped_to_the_maximum():
    assert compute_retry_backoff(30.0, 99) == DEFAULT_RETRY_BACKOFF_MAX_S
    # A base already above the cap is clamped on its very first retry.
    assert compute_retry_backoff(DEFAULT_RETRY_BACKOFF_MAX_S * 2, 1) == DEFAULT_RETRY_BACKOFF_MAX_S


def test_zero_base_means_retry_immediately():
    """Not rejected here -- `api/settings.py` owns input validation. This only pins that the
    arithmetic itself stays well-defined rather than producing a negative or a surprise.
    """
    assert compute_retry_backoff(0.0, 1) == 0.0
    assert compute_retry_backoff(0.0, 5) == 0.0
