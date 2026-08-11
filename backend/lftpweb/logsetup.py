"""App-log setup (DESIGN.md §10.1): rotating file handler + console, credentials redacted
before anything is written to disk — not before display, because a secret that reaches
disk has already leaked.
"""

from __future__ import annotations

import logging
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path

MAX_BYTES = 5 * 1024 * 1024  # 5 MB
BACKUP_COUNT = 5  # 5 files -> 25 MB ceiling

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


def setup_logging(config_dir: str, log_level: str) -> None:
    """Configure the root logger with a rotating file handler and a console handler."""
    log_dir = Path(config_dir) / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    formatter = logging.Formatter(_LOG_FORMAT)
    redactor = CredentialRedactor()

    file_handler = RotatingFileHandler(
        log_dir / "lftpweb.log", maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT
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
