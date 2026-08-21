"""The production sequence that this defect actually is, end to end through a real scan pass
and a real `AutoQueue.on_scan` -- not a pure-function assertion about either half.

`prompts/done/2026-08-19-autoqueue-requeues-imported-item.md`, support bundle
`lftpweb-support-0.2.6-20260819T205145Z.zip` (production, v0.2.6, `move` queue `dc-tv` bound to
Sonarr). Item 3354306:

| 18:15:23 | job 391 succeeds, 1,651,731,114 bytes                                       |
| 18:15:24 | verify `SKIPPED` (so the item rests `DOWNLOADED`), renamed to its final name |
| 18:15:24 | `arr_notified` -- Sonarr told to scan; `remote_delete_deferred`             |
| ~18:15:5x | Sonarr moves the media file out (its 90-second retry loop for this path stops) |
| 18:16:06 | **`auto-queue: queued 1 item(s)`** -> job 395                                |
| 18:17:32 | `remote_delete` -- source deleted on confirmed import                        |
| 18:21:44 | job 395 admitted -> `REMOTE_GONE`, 0 bytes, having blocked cleanup throughout |

The residual `.nfo` is what makes the reading `PARTIAL` rather than `REMOTE_ONLY`, and `PARTIAL`
had no grace-period protection at all -- that is the whole defect. The guard test at the bottom
is the one that matters most: it proves the fix did not buy this by making a genuinely
interrupted transfer un-resumable, which would be far worse than the bug.
"""

from __future__ import annotations

import aiosqlite
import pytest

import lftpweb.core.engine as engine_module
from lftpweb.core.autoqueue import AutoQueue, QueueAutoConfig
from lftpweb.core.engine import Engine, QueueConfig
from lftpweb.core.events import EventBus
from lftpweb.core.local_scan import LocalEntry
from lftpweb.core.mount_sentinel import DEFAULT_GRACE_S, write_if_needed
from lftpweb.core.remote import HostConfig, RemoteEntry
from lftpweb.core.settle import SettleSettings, save_settle_settings
from lftpweb.db import migrate

RELEASE = "Married.At.First.Sight.S12E15.720p.WEB.h264-BAE"
MEDIA = f"{RELEASE}/episode.mkv"
NFO = f"{RELEASE}/episode.nfo"
MEDIA_BYTES = 1_651_731_114
NFO_BYTES = 1_024


class _FakePool:
    def __init__(self, tree):
        self._tree = tree

    async def scan(self, host, remote_path):  # noqa: ARG002 - matches RemoteConnectionPool.scan
        return self._tree, None


class _FakePipeline:
    def in_flight_item_ids(self) -> frozenset[int]:
        return frozenset()


def _remote_tree() -> dict[str, RemoteEntry]:
    return {
        RELEASE: RemoteEntry(RELEASE, True, 0, 1.0),
        MEDIA: RemoteEntry(MEDIA, False, MEDIA_BYTES, 1.0),
        NFO: RemoteEntry(NFO, False, NFO_BYTES, 1.0),
    }


def _local_tree(*, media: int | None, nfo: int | None) -> dict[str, LocalEntry]:
    """`None` for either file = that file is not on disk. The import case is
    `media=None, nfo=NFO_BYTES`; an interrupted transfer is `media=<short>, nfo=NFO_BYTES`.
    """
    tree: dict[str, LocalEntry] = {RELEASE: LocalEntry(RELEASE, True)}
    if media is not None:
        tree[MEDIA] = LocalEntry(MEDIA, False, media)
    if nfo is not None:
        tree[NFO] = LocalEntry(NFO, False, nfo)
    return tree


class _Recorder:
    def __init__(self):
        self.enqueued: list[int] = []

    async def __call__(self, item_id: int) -> int:
        self.enqueued.append(item_id)
        return item_id


class _Harness:
    """One `move` queue, one release, a scan pass and an auto-queue pass over the same db --
    the pair of moving parts the defect lives between.
    """

    def __init__(self, db, engine, queue, host, auto_config, recorder):
        self.db = db
        self.engine = engine
        self.queue = queue
        self.host = host
        self.auto_config = auto_config
        self.recorder = recorder
        self.auto_queue = AutoQueue(db, recorder)

    async def scan(self) -> None:
        await self.engine.scan_queue(self.queue, self.host)

    async def auto_queue_pass(self) -> int:
        return await self.auto_queue.on_scan(self.auto_config)

    async def item(self) -> aiosqlite.Row:
        cursor = await self.db.execute(
            "SELECT id, state, first_missing_at FROM item WHERE rel_path = ?", (RELEASE,)
        )
        return await cursor.fetchone()


@pytest.fixture
async def harness(tmp_path, monkeypatch):
    db = await aiosqlite.connect(":memory:")
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA foreign_keys = ON")
    await migrate(db)

    cursor = await db.execute(
        "INSERT INTO host (name, address, port, username, auth_method, known_hosts_policy) "
        "VALUES ('h', '127.0.0.1', 22, 'u', 'key', 'strict')"
    )
    host_id = cursor.lastrowid
    cursor = await db.execute(
        "INSERT INTO path_queue (host_id, name, remote_path, local_path, enabled, sync_mode, "
        "auto_queue_enabled) VALUES (?, 'dc-tv', '/remote', ?, 1, 'move', 1)",
        (host_id, str(tmp_path)),
    )
    queue_id = cursor.lastrowid
    await db.commit()

    # The settle gate would otherwise hold a first-scan DOWNLOADED at REMOTE_ONLY/settling for
    # its first couple of passes, which has nothing to do with what this file tests (and the
    # production queue's own gate had long since cleared this release).
    await save_settle_settings(db, SettleSettings(enabled=False))

    # Local presence is driven per-test by rebinding this attribute.
    state = {"local": _local_tree(media=MEDIA_BYTES, nfo=NFO_BYTES)}
    monkeypatch.setattr(
        engine_module.local_scan,
        "scan_local",
        lambda root, **_kwargs: state["local"],  # noqa: ARG005
    )
    write_if_needed(str(tmp_path))

    engine = Engine(db, str(tmp_path), EventBus())
    engine.pool = _FakePool(_remote_tree())
    engine.postprocess = _FakePipeline()
    queue = QueueConfig(
        id=queue_id,
        host_id=host_id,
        name="dc-tv",
        remote_path="/remote",
        local_path=str(tmp_path),
        staging_path=None,
        enabled=True,
        sync_mode="move",
    )
    host = HostConfig(
        id=host_id,
        address="127.0.0.1",
        port=22,
        username="u",
        auth_method="key",
        key_path="/k",
        known_hosts_policy="strict",
    )
    auto_config = QueueAutoConfig(
        id=queue_id,
        name="dc-tv",
        local_path=str(tmp_path),
        auto_queue_enabled=True,
        patterns_only=False,
    )
    h = _Harness(db, engine, queue, host, auto_config, _Recorder())
    h.local = state
    yield h
    await db.close()


async def test_an_importer_taking_the_release_apart_does_not_trigger_a_re_queue(harness):
    """The regression. Fails against the pre-2026-08-19 code at the final assertion: the
    leftover `.nfo` makes the directory read `PARTIAL`, `PARTIAL` was unconditionally
    auto-queue-eligible, and a job was spawned for a release whose source was about to be
    deleted on confirmed import.
    """
    await harness.scan()
    assert (await harness.item())["state"] == "DOWNLOADED"
    assert await harness.auto_queue_pass() == 0, "a complete item is not eligible to begin with"

    # Sonarr moves the media file into the library and leaves the release directory behind.
    harness.local["local"] = _local_tree(media=None, nfo=NFO_BYTES)
    await harness.scan()

    row = await harness.item()
    assert row["state"] == "DOWNLOADED", "held for the grace period, not published as PARTIAL"
    assert row["first_missing_at"] is not None, "the grace clock should be running"

    assert await harness.auto_queue_pass() == 0
    assert harness.recorder.enqueued == []


async def test_the_arr_hand_off_keeps_covering_it_after_the_grace_window_expires(harness):
    """The grace period is a ten-minute window; a season-pack import in the same incident took
    ~19 minutes. Once the window lapses the item is released to `PARTIAL` on purpose (the truth
    about the disk), so on an *arr-bound queue the second half of the fix -- `arr_status` past
    the hand-off -- is what keeps it out of auto-queue with no time bound at all.
    """
    await harness.scan()
    item_id = (await harness.item())["id"]
    # `core/postprocess.py`'s tail pushed the scan command for this release, exactly as the
    # bundle's 18:15:24 `arr_notified` event records.
    await harness.db.execute("UPDATE item SET arr_status = 'notified' WHERE id = ?", (item_id,))
    await harness.db.commit()

    harness.local["local"] = _local_tree(media=None, nfo=NFO_BYTES)
    await harness.scan()
    await harness.db.execute(
        "UPDATE item SET first_missing_at = STRFTIME('%Y-%m-%dT%H:%M:%fZ', 'now', ?) WHERE id = ?",
        (f"-{int(DEFAULT_GRACE_S) + 60} seconds", item_id),
    )
    await harness.db.commit()

    await harness.scan()
    assert (await harness.item())["state"] == "PARTIAL", "the window lapsed; PARTIAL is the truth"
    assert await harness.auto_queue_pass() == 0
    assert harness.recorder.enqueued == []


async def test_a_genuinely_interrupted_transfer_is_still_re_queued(harness):
    """**The guard.** `PARTIAL` being eligible is how an interrupted transfer resumes -- lftp
    picks up from what is already on disk (`mirror -c`). This item never reached a complete
    state, so nothing about the shrink grace period may touch it: it must read `PARTIAL` on the
    very first scan after the interruption and be picked straight back up.
    """
    harness.local["local"] = _local_tree(media=MEDIA_BYTES // 3, nfo=NFO_BYTES)
    await harness.scan()

    row = await harness.item()
    assert row["state"] == "PARTIAL"
    assert row["first_missing_at"] is None, "nothing went missing; no clock may be running"

    assert await harness.auto_queue_pass() == 1
    assert harness.recorder.enqueued == [row["id"]]


async def test_an_interrupted_transfer_of_a_previously_complete_item_still_resumes(harness):
    """The second pass of the same interruption -- the item is now persisted `PARTIAL`, so even
    if it had once been complete the shrink key no longer applies and it stays re-queueable
    every pass until it finishes.
    """
    harness.local["local"] = _local_tree(media=MEDIA_BYTES // 3, nfo=NFO_BYTES)
    await harness.scan()
    await harness.scan()

    assert (await harness.item())["state"] == "PARTIAL"
    assert await harness.auto_queue_pass() == 1


async def test_a_remote_that_grew_after_completion_is_re_queued_immediately(harness):
    """§3.2 rule 4, the case the shrink key must never swallow: a completed release gains a new
    file upstream. There genuinely is more to fetch, so no hold -- `PARTIAL` on the first pass
    and eligible on the same pass.
    """
    await harness.scan()
    assert (await harness.item())["state"] == "DOWNLOADED"

    grown = _remote_tree()
    extra = f"{RELEASE}/episode.sample.mkv"
    grown[extra] = RemoteEntry(extra, False, 4_096, 1.0)
    harness.engine.pool = _FakePool(grown)

    await harness.scan()
    row = await harness.item()
    assert row["state"] == "PARTIAL"
    assert row["first_missing_at"] is None
    assert await harness.auto_queue_pass() == 1
    assert harness.recorder.enqueued == [row["id"]]
