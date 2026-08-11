from __future__ import annotations

from lftpweb.core.remote import _ensure_local_username_env


def test_sets_logname_when_nothing_identifies_the_user(monkeypatch):
    for name in ("LOGNAME", "USER", "LNAME", "USERNAME"):
        monkeypatch.delenv(name, raising=False)

    _ensure_local_username_env()

    assert __import__("os").environ["LOGNAME"] == "lftpweb"


def test_does_not_override_an_existing_username(monkeypatch):
    # A real environment variable must win — this is a fallback, not an override. Found
    # while running lftpweb in its own container (numeric uid, no /etc/passwd entry):
    # asyncssh.connect() calls getpass.getuser() unconditionally for SSH-config username
    # templating, which raises OSError on Python 3.13 when neither the environment nor
    # /etc/passwd can identify the running uid — see the comment on the call site.
    for name in ("LOGNAME", "USER", "LNAME", "USERNAME"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("USER", "realuser")

    _ensure_local_username_env()

    assert __import__("os").environ.get("LOGNAME") is None
    assert __import__("os").environ["USER"] == "realuser"
