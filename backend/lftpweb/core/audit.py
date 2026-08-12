"""Durable audit trail for irreversible actions -- DESIGN.md §3.1's `event` table.

Distinct from `core/events.py`'s `EventBus` despite the name overlap: that one is an
in-process, non-persisted WebSocket fan-out (model-change notifications for connected
browsers); this one is the queryable database table the History page (phase 6) will read and
that DESIGN.md §7.3/§7.4 requires for every remote delete. The two are unrelated on purpose --
see `core/events.py`'s own module docstring.

First real writer: `core/postprocess.py`'s `move`-mode delete gate (§7.3: "every delete -- and
every delete withheld, with the failing precondition -- writes an event row"). Kept as its own
tiny module rather than folded into `postprocess.py` so a future writer (phase 6, or a
user-initiated Files-view delete) doesn't have to import the whole postprocessing pipeline
just to log an event.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import aiosqlite

logger = logging.getLogger(__name__)

_LOG_FUNCS = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
}


async def record_event(
    db: aiosqlite.Connection,
    *,
    level: str,
    kind: str,
    message: str,
    item_id: int | None = None,
    job_id: int | None = None,
) -> None:
    """Insert one `event` row and commit immediately -- an audit record that isn't durable
    the instant it's produced isn't an audit record. Also logged at `level` through the
    normal app logger (DESIGN.md §10.1) so an operator tailing logs sees it too, not only
    someone who later opens the History page.
    """
    await db.execute(
        "INSERT INTO event (ts, level, item_id, job_id, kind, message) VALUES (?, ?, ?, ?, ?, ?)",
        (
            datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            level,
            item_id,
            job_id,
            kind,
            message,
        ),
    )
    await db.commit()
    logger.log(
        _LOG_FUNCS.get(level, logging.INFO),
        "event[%s] item=%s job=%s: %s",
        kind,
        item_id,
        job_id,
        message,
    )
