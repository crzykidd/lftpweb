"""App-log setup (DESIGN.md §10.1): rotating file handler + console, credentials redacted
before anything is written to disk — not before display, because a secret that reaches
disk has already leaked.
"""

from __future__ import annotations

import logging
import os
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path

MAX_BYTES = 5 * 1024 * 1024  # 5 MB
BACKUP_COUNT = 5  # 5 files -> 25 MB ceiling
LOG_FILENAME = "lftpweb.log"  # named here so api/logs.py never hardcodes it separately

# Third-party loggers that inherit the *root* level, and so light up in full whenever
# LFTPWEB_LOG_LEVEL is lowered to debug this application's own code. Measured on a running
# dev instance: 37,388 aiosqlite lines (every statement logged twice, and the scheduler polls
# settings once a second) against exactly 1 line from lftpweb itself. That is not merely
# unreadable — the handler below has a fixed 25 MB budget, so library chatter actively evicts
# the lines an incident would need, and Settings -> Logs ends up tailing noise.
#
# Each entry is a *floor*, never a ceiling: a quieter root level still wins (see the max()
# below), so this can only suppress, never force output the operator asked to be rid of.
_THIRD_PARTY_FLOORS = {
    "aiosqlite": logging.WARNING,
    "asyncssh": logging.WARNING,
    "websockets": logging.WARNING,
}

# The escape hatch, and it is load-bearing rather than a courtesy: asyncssh's own output is how
# connection problems get diagnosed here (it is what distinguishes "the pooled connection is
# being reused" from "we reconnect every scan"), and this project's hardest bugs were found by
# reading transport-level logs. `LFTPWEB_DEBUG_LIBS=asyncssh,aiosqlite` drops the floor for the
# named loggers so they follow the root level again.
#
# Read from the environment here rather than threaded through `config.py`'s Settings: logging is
# configured before the app (and therefore before Settings) is built, and this keeps the knob in
# the same module as the behaviour it controls.
DEBUG_LIBS_ENV = "LFTPWEB_DEBUG_LIBS"

_LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"

# scheme://user:pass@host -> scheme://user:***@host (DESIGN.md §4.2). Only a stub today —
# there are no credentials in the system yet — but it's wired into every handler now so
# nothing has to remember to add it later.
_CREDENTIAL_RE = re.compile(
    r"(?P<scheme>[a-zA-Z][a-zA-Z0-9+.\-]*://)(?P<user>[^:/@\s]+):(?P<password>[^@/\s]+)@"
)


class CredentialRedactor(logging.Filter):
    """Logging filter that redacts `user:pass@` credentials embedded in URLs."""

    def filter(self, record: logging.LogRecord) -> bool:
        # Render the message now (applying %-args), then redact and stop args from being
        # applied a second time by the formatter.
        record.msg = self.redact(record.getMessage())
        record.args = ()
        return True

    @staticmethod
    def redact(text: str) -> str:
        return _CREDENTIAL_RE.sub(lambda m: f"{m.group('scheme')}{m.group('user')}:***@", text)


# Endpoints the UI polls on a timer. At ~1 Hz these access-log lines bury everything that
# matters — a real deployment's log was wall-to-wall "GET /api/stats 200 OK" with the job
# lifecycle nowhere to be seen. Dropped from the access log only; the requests still serve
# normally, and any non-2xx response is kept because that is a real signal.
_POLLED_PATHS = ("/api/stats", "/api/health", "/api/ws")


class PollingNoiseFilter(logging.Filter):
    """Drop successful uvicorn access records for endpoints the UI polls continuously."""

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        if not any(path in message for path in _POLLED_PATHS):
            return True
        # Keep anything that isn't a plain success — errors are worth seeing even here.
        return " 2" not in message and "accepted" not in message


def setup_logging(config_dir: str, log_level: str) -> None:
    """Configure the root logger with a rotating file handler and a console handler."""
    log_dir = Path(config_dir) / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    formatter = logging.Formatter(_LOG_FORMAT)
    redactor = CredentialRedactor()

    file_handler = RotatingFileHandler(
        log_dir / LOG_FILENAME, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT
    )
    file_handler.setFormatter(formatter)
    file_handler.addFilter(redactor)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    console_handler.addFilter(redactor)

    root = logging.getLogger()
    root.setLevel(log_level.upper())
    root.handlers.clear()
    root.addHandler(file_handler)
    root.addHandler(console_handler)

    logging.getLogger("uvicorn.access").addFilter(PollingNoiseFilter())
    _apply_third_party_floors(root.level)


def _apply_third_party_floors(root_level: int) -> None:
    """Keep library loggers from drowning this application's own output (see
    `_THIRD_PARTY_FLOORS`). Same intent as the `PollingNoiseFilter` above, one level up: that
    one drops individual noisy records, this one stops whole libraries inheriting a debug root.
    """
    verbose = {
        name.strip().lower()
        for name in os.environ.get(DEBUG_LIBS_ENV, "").split(",")
        if name.strip()
    }
    for name, floor in _THIRD_PARTY_FLOORS.items():
        logger = logging.getLogger(name)
        if name in verbose:
            # NOTSET means "inherit", so the logger follows the root level again — this is an
            # opt-in to the full firehose, which is exactly what a connection bug wants.
            logger.setLevel(logging.NOTSET)
        else:
            # max(): levels are numerically higher when *less* verbose, so a root of ERROR
            # stays ERROR rather than being loosened back to WARNING by this floor.
            logger.setLevel(max(floor, root_level))
