from __future__ import annotations

from lftpweb.logsetup import CredentialRedactor


def test_redacts_password_in_sftp_url():
    assert (
        CredentialRedactor.redact("sftp://user:secret@host")
        == "sftp://user:***@host"
    )


def test_leaves_credential_free_text_untouched():
    text = "connected to sftp://host without credentials in the url"
    assert CredentialRedactor.redact(text) == text
