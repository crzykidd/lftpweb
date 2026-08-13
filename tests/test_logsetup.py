from __future__ import annotations

import logging

from lftpweb.logsetup import CredentialRedactor


def test_redacts_password_in_sftp_url():
    assert CredentialRedactor.redact("sftp://user:secret@host") == "sftp://user:***@host"


def test_leaves_credential_free_text_untouched():
    text = "connected to sftp://host without credentials in the url"
    assert CredentialRedactor.redact(text) == text


# --- migration 014: a pasted private key's multi-line PEM form (DESIGN.md §8) --------------

_FAKE_OPENSSH_KEY = (
    "-----BEGIN OPENSSH PRIVATE KEY-----\n"
    "b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAABAAAAMwAAAAtzc2gt\n"
    "ZWQyNTUxOQAAACBsb21lc2VjcmV0a2V5bWF0ZXJpYWxub3RyZWFsMTIzNDU2Nzg5MA\n"
    "AAAEAllAlreadyLooksLikeALineOfBase64ButIsNotARealKeyAtAllHonest\n"
    "-----END OPENSSH PRIVATE KEY-----\n"
)

_FAKE_RSA_KEY = (
    "-----BEGIN RSA PRIVATE KEY-----\n"
    "MIIEowIBAAKCAQEAtotallyfakebase64thatlooksplausibleenoughforatest\n"
    "-----END RSA PRIVATE KEY-----\n"
)


def test_redacts_a_multiline_openssh_private_key():
    text = f"some log line\n{_FAKE_OPENSSH_KEY}more log output"
    redacted = CredentialRedactor.redact(text)

    assert "BEGIN OPENSSH PRIVATE KEY" in redacted  # the label itself is fine to show
    assert "END OPENSSH PRIVATE KEY" in redacted
    assert "***REDACTED***" in redacted
    # None of the base64 body lines survive -- not "the first line is scrubbed, the rest
    # leak," which is exactly the failure mode a single-line-only redactor would have.
    for line in _FAKE_OPENSSH_KEY.splitlines()[1:-1]:
        assert line not in redacted
    assert "some log line" in redacted
    assert "more log output" in redacted


def test_redacts_an_rsa_private_key_label_too():
    # The label varies (`OPENSSH PRIVATE KEY`, `RSA PRIVATE KEY`, `PRIVATE KEY` for PKCS8...);
    # the pattern must not be hardcoded to one.
    redacted = CredentialRedactor.redact(_FAKE_RSA_KEY)
    assert "totallyfakebase64" not in redacted
    assert "***REDACTED***" in redacted


def test_two_distinct_keys_logged_back_to_back_are_each_redacted_separately():
    text = _FAKE_OPENSSH_KEY + _FAKE_RSA_KEY
    redacted = CredentialRedactor.redact(text)
    assert "totallyfakebase64" not in redacted
    assert "b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQ" not in redacted
    # Two redaction markers, not one match that swallowed both blocks and everything between.
    assert redacted.count("***REDACTED***") == 2


def test_credential_redactor_filter_scrubs_a_multiline_log_record(caplog):
    """End to end through the actual logging filter, not just the static `redact()` helper --
    proves a real `logger.info(...)` call carrying a pasted key never reaches a handler
    un-redacted, which is what actually matters (DESIGN.md §4.2's "redacted on the way into
    logs, not before display").
    """
    logger = logging.getLogger("lftpweb.test.key_redaction")
    logger.addFilter(CredentialRedactor())
    with caplog.at_level(logging.INFO, logger="lftpweb.test.key_redaction"):
        logger.info("accidental key dump: %s", _FAKE_OPENSSH_KEY)
    assert len(caplog.records) == 1
    message = caplog.records[0].getMessage()
    assert "b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQ" not in message
    assert "***REDACTED***" in message
