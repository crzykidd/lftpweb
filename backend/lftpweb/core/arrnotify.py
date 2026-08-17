"""The *arr push-notify ("your files are here, import now") -- docs/arr-integration-spec.md
"Notify". One implementation, shared by its two callers:

- `core/postprocess.py.PostprocessPipeline._maybe_notify_arr` -- the *primary* attempt, fired
  once from the pipeline's own tail, only after the whole pipeline succeeds (spec: "after the
  whole pipeline succeeds").
- `core/arrsync.py.ArrSyncScheduler._maybe_retry_notify` -- the *bounded retry*, on later
  poller passes, for a primary attempt that failed (spec: "Notify failure is non-fatal: event
  row + retry on the next poller tick (bounded retries)").

Kept as its own module, not folded into either caller, so there is exactly one place that
builds the *arr POST, translates the path, and writes the `arr_notified`/`arr_notify_failed`
event -- the same "narrow module, no second implementation" shape `core/audit.py` describes for
itself.

**Gated on `item.arr_status == 'detected'`** -- the lifecycle diagram (spec "The association
lifecycle") shows `notified` reachable only from `detected`, never from `(no status)` directly.
An item lftpweb hasn't matched against the bound instance's own queue yet has nothing to notify
about -- pushing a scan command for a release the *arr's queue listing doesn't (yet) know about
is exactly the kind of ambiguous action this feature's safety rules ask to avoid, even though
notify itself is non-destructive.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import aiosqlite

from lftpweb.core import audit
from lftpweb.core.arrclient import ArrClient, ArrClientError
from lftpweb.core.crypto import DecryptionError, decrypt_secret
from lftpweb.core.events import EventBus
from lftpweb.core.itemview import item_view

logger = logging.getLogger(__name__)

NotifyOutcome = Literal["not_configured", "not_detected", "notified", "failed"]


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def translate_to_arr_namespace(
    final_local_path: str,
    *,
    local_path: str,
    staging_path: str | None,
    arr_visible_path: str | None,
) -> str:
    """Path namespaces (spec "Path namespaces"): the notify path is the item's final physical
    path with the queue's local root prefix replaced by `arr_visible_path`. `NULL` (`None`
    here) means "same namespace" -- send our path unchanged.

    **Two candidate prefixes, not one** -- the spec's own note: "if the queue's Move step
    relocates to `staging_path`, `arr_visible_path` describes where *that* lands in the *arr's
    view." A queue with `auto_move` on relocates an item's final resting place from `local_path`
    to `staging_path` (`core/postprocess.py._do_move`), so the prefix actually present in
    `final_local_path` is `staging_path` for such an item, not `local_path` -- checked first
    here for exactly that reason (a move-mode item's final path is never under `local_path` any
    more once the move has happened). A queue with no `staging_path` (the common case) only ever
    has one candidate, so this degrades to the simple single-prefix substitution for it.

    Defensive fallback: if `final_local_path` isn't actually under either root (should never
    happen -- every caller derives it from one of this queue's own roots), send it unchanged
    rather than guess at a substring match -- "any path lftpweb sends to the *arr must be in
    namespace 3" is a rule about *correctness*, not about papering over a path this function
    cannot make sense of.
    """
    if arr_visible_path is None:
        return final_local_path
    visible = arr_visible_path.rstrip("/")
    for candidate_prefix in (staging_path, local_path):
        if not candidate_prefix:
            continue
        prefix = candidate_prefix.rstrip("/")
        if final_local_path == prefix:
            return visible
        if final_local_path.startswith(prefix + "/"):
            return visible + final_local_path[len(prefix) :]
    logger.warning(
        "*arr notify: %r is not under queue local_path %r or staging_path %r -- sending "
        "unchanged rather than guessing a translation",
        final_local_path,
        local_path,
        staging_path,
    )
    return final_local_path


async def _publish(
    db: aiosqlite.Connection, events: EventBus | None, queue_id: int, item_id: int
) -> None:
    if events is None:
        return
    cursor = await db.execute("SELECT * FROM item WHERE id = ?", (item_id,))
    row = await cursor.fetchone()
    if row is not None:
        events.publish({"type": "item_delta", "queue_id": queue_id, "nodes": [item_view(row)]})


async def notify_arr(
    db: aiosqlite.Connection,
    *,
    config_dir: str,
    item: Any,
    queue: Any,
    final_local_root: Path,
    events: EventBus | None = None,
) -> NotifyOutcome:
    """POST the *arr scan command (`DownloadedEpisodesScan`/`DownloadedMoviesScan`,
    `importMode: "Copy"` -- `core/arrclient.py.ArrClient.post_scan_command`) for `item`'s final
    physical location, translated into the *arr's own namespace.

    `"not_configured"` -- no instance bound, the instance is disabled, or `notify_on_complete`
    is off. `"not_detected"` -- `item.arr_status != 'detected'` (module docstring). Neither
    writes an event: this project's "everything defaults off" rule means a queue that never
    opted in produces zero events, not a stream of "nothing to do" noise.

    `"notified"` -- the push succeeded: `arr_status = 'notified'` + `arr_notified` event, and
    (2026-08-17) the pushed command's own id recorded in `item.arr_scan_command_id` so
    `core/arrsync.py`'s poller can later confirm the *arr actually finished it, not just
    accepted it.
    `"failed"` -- the instance could not be reached, returned a non-2xx, or its stored API key
    could no longer be decrypted: `arr_notify_failed` event, `arr_status` left at `'detected'`
    so `core/arrsync.py`'s bounded retry can try again on a later pass. Never raises -- every
    failure this function can hit is folded into the `"failed"` return, same as
    `core/arrsync.py`'s own per-instance failure isolation.
    """
    if queue["arr_instance_id"] is None:
        return "not_configured"
    if item["arr_status"] != "detected":
        return "not_detected"

    cursor = await db.execute(
        "SELECT id, name, kind, base_url, api_key_enc, enabled, notify_on_complete "
        "FROM arr_instance WHERE id = ?",
        (queue["arr_instance_id"],),
    )
    instance = await cursor.fetchone()
    if instance is None or not instance["enabled"] or not instance["notify_on_complete"]:
        return "not_configured"

    arr_path = translate_to_arr_namespace(
        str(final_local_root),
        local_path=queue["local_path"],
        staging_path=queue["staging_path"],
        arr_visible_path=queue["arr_visible_path"],
    )

    async def _fail(reason: str) -> NotifyOutcome:
        await audit.record_event(
            db,
            level="warning",
            item_id=item["id"],
            kind="arr_notify_failed",
            message=(
                f"queue {queue['id']} ({queue['name']!r}): push to *arr instance "
                f"{instance['name']!r} for {arr_path!r} failed -- {reason}"
            ),
        )
        return "failed"

    try:
        api_key = decrypt_secret(config_dir, instance["api_key_enc"])
    except DecryptionError as exc:
        return await _fail(f"could not decrypt stored API key: {exc}")

    try:
        async with ArrClient(
            kind=instance["kind"], base_url=instance["base_url"], api_key=api_key
        ) as client:
            command = await client.post_scan_command(arr_path)
    except ArrClientError as exc:
        return await _fail(str(exc))

    # `id` (2026-08-17, scan-command outcome verification) -- the 201 above only means "command
    # queued", not "the *arr could act on this path"; `core/arrsync.py`'s poller polls this id
    # (`get_command`) on later passes to close that gap. `None` if the response body is missing
    # or unexpectedly shaped -- degrades to "no outcome check happens for this item," never a
    # notify failure of its own (the push itself still succeeded).
    command_id = command.get("id") if isinstance(command, dict) else None
    await db.execute(
        "UPDATE item SET arr_status = 'notified', arr_status_at = ?, arr_scan_command_id = ? "
        "WHERE id = ?",
        (_now_iso(), command_id, item["id"]),
    )
    await db.commit()
    await audit.record_event(
        db,
        level="info",
        item_id=item["id"],
        kind="arr_notified",
        message=(
            f"queue {queue['id']} ({queue['name']!r}): pushed *arr scan command for "
            f"{arr_path!r} to instance {instance['name']!r}"
        ),
    )
    await _publish(db, events, queue["id"], item["id"])
    return "notified"
