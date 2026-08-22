"""Tests for `core/clients/capture.py` (docs/download-client-framework-spec.md §13.3) -- the
redacted response-capture helper every connector's `test_connection` uses, and the one the
rTorrent connector (stage 2+) will reuse for announce-URL redaction (spec §7.3).
"""

from __future__ import annotations

from lftpweb.core.clients.capture import (
    DEFAULT_CAPTURE_BYTE_CAP,
    cap_sample,
    capture_response,
    redact_announce_url,
    redact_secret,
)

SECRET = "s3cr3t-api-key-value"  # noqa: S105 - test-only literal, never a real credential


def test_redact_secret_in_a_query_string():
    text = f"GET /api?mode=version&apikey={SECRET}&output=json"
    redacted = redact_secret(text, secret=SECRET)
    assert SECRET not in redacted
    assert "***REDACTED***" in redacted


def test_redact_secret_in_a_body():
    text = f'{{"apikey": "{SECRET}", "mode": "queue"}}'
    redacted = redact_secret(text, secret=SECRET)
    assert SECRET not in redacted


def test_redact_secret_appearing_twice_in_one_string():
    text = f"GET /api?apikey={SECRET} -> body echoed apikey={SECRET} back"
    redacted = redact_secret(text, secret=SECRET)
    assert SECRET not in redacted
    assert redacted.count("***REDACTED***") == 2


def test_redact_secret_empty_secret_is_a_no_op():
    text = "nothing secret here"
    assert redact_secret(text, secret="") == text


def test_redact_secret_absent_from_text_is_unchanged_besides_no_match():
    text = "no secret in here at all"
    assert redact_secret(text, secret=SECRET) == text


def test_redact_announce_url_keeps_only_scheme_and_host():
    url = "http://tracker.example.test:6969/announce?passkey=abcd1234secretpasskey"
    assert redact_announce_url(url) == "http://tracker.example.test:6969"


def test_redact_announce_url_strips_a_passkey_in_the_path():
    url = "https://tracker.example.test/abcd1234secretpasskey/announce"
    result = redact_announce_url(url)
    assert result == "https://tracker.example.test"
    assert "abcd1234secretpasskey" not in result


def test_redact_announce_url_tolerates_a_malformed_url_without_raising():
    garbage = "not a url at all :: 12345"
    assert redact_announce_url(garbage) == garbage


def test_cap_sample_leaves_a_short_string_untouched():
    text = "short response body"
    assert cap_sample(text, max_bytes=4096) == text


def test_cap_sample_truncates_a_long_string():
    text = "x" * 10_000
    capped = cap_sample(text, max_bytes=100)
    assert len(capped.encode("utf-8")) > 100  # the marker itself adds a few bytes back
    assert capped.startswith("x" * 100)
    assert "truncated" in capped


def test_cap_sample_default_cap_is_a_real_positive_number():
    assert DEFAULT_CAPTURE_BYTE_CAP > 0


def test_capture_response_redacts_before_capping_so_a_boundary_split_secret_never_leaks():
    # The secret sits right at the naive truncation boundary -- if capping ran before
    # redaction, a truncated half-secret could survive in the output. Redacting first
    # collapses it to a fixed-width marker well before the cap is ever applied.
    prefix = "x" * 50
    text = f"{prefix}{SECRET}{'y' * 50}"
    result = capture_response(text, secrets=(SECRET,), max_bytes=60)
    assert SECRET not in result
    assert SECRET[: len(SECRET) // 2] not in result


def test_capture_response_redacts_multiple_secrets():
    other_secret = "another-secret-value"  # noqa: S105
    text = f"apikey={SECRET} other={other_secret}"
    result = capture_response(text, secrets=(SECRET, other_secret))
    assert SECRET not in result
    assert other_secret not in result


def test_capture_response_with_no_secrets_only_caps():
    text = "plain diagnostic text, nothing secret"
    assert capture_response(text) == text
