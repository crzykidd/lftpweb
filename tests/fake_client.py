"""A fake download-client connector for the conformance suite and the capability-merge unit
tests (docs/download-client-framework-spec.md §6.2, §13.1) -- same philosophy as
`tests/fake_arr.py`: a real class implementing the real `DownloadClient` ABC, not a mock, so
the conformance suite exercises the actual method-resolution and capability-declaration
machinery a real adapter goes through, not a stand-in for it.

No real socket is needed at this stage (unlike `fake_arr.py`'s real `uvicorn` listener) --
stage 0 never contacts a client at all, by design (`docs/download-client-framework-spec.md`
§14). This fixture is kept in the same "mutable state object a test drives directly, in-process,
between passes" shape `fake_arr.py`'s `FakeArrState` uses, specifically so stage 1's fake
SABnzbd/fake rTorrent fixtures (spec §13.1, which *do* need a real socket) can follow the
identical pattern without this one needing to be reshaped first.

Registered under `"fake"` here -- in `tests/`, not in `core/clients/` -- because it is test-only
scaffolding that must never ship in the production image or appear in a real deployment's
registry (`core/clients/__init__.py`'s own module docstring: "it is never imported here, so it
never reaches the production registry a real deployment sees").
"""

from __future__ import annotations

from dataclasses import dataclass, field

from lftpweb.core.clients import TORRENT_BASELINE, register_client
from lftpweb.core.clients.base import (
    Capability,
    ConfigField,
    DownloadClient,
    Field,
    Operation,
    Support,
    project_transfer,
)
from lftpweb.core.clients.errors import CapabilityUnavailable, ClientUnreachable
from lftpweb.core.clients.models import (
    BasePath,
    ConnectionInfo,
    RemoveOutcome,
    SpaceInfo,
    Transfer,
    TrackerInfo,
    TransferPhase,
)

# A deliberately small, deliberately *not exhaustive* mapping -- `map_phase`'s whole point is
# that anything not in here still returns `UNKNOWN` rather than raising (spec §3).
_PHASE_MAP: dict[str, TransferPhase] = {
    "downloading": TransferPhase.DOWNLOADING,
    "seeding": TransferPhase.SEEDING,
    "paused": TransferPhase.PAUSED,
    "checking": TransferPhase.VERIFYING,
    "complete": TransferPhase.COMPLETED,
    "error": TransferPhase.FAILED,
}


@dataclass
class FakeClientState:
    """The mutable store a test manipulates directly between calls -- `fake_arr.py`'s
    `FakeArrState` shape, one notch simpler since there is no real socket in stage 0.
    """

    reachable: bool = True
    transfers: list[Transfer] = field(default_factory=list)
    base_paths: list[BasePath] = field(default_factory=list)
    free_bytes: int = 0
    # Keyed by `Operation` -- when present, that operation raises `CapabilityUnavailable` the
    # next call, regardless of what the static declaration says. This is the "declares a
    # capability and then fails it at runtime" hook spec §4.1 layer 3 needs
    # (docs/download-client-framework-spec.md §4.1): a fake adapter must be able to model a
    # plugin that vanished, or a deployment quirk the static declaration can't foresee, so the
    # degradation path is testable without a real client at all.
    fail_operations: set[Operation] = field(default_factory=set)


@register_client("fake")
class FakeDownloadClient(DownloadClient):
    """A torrent-shaped fake -- `TORRENT_BASELINE` unmodified, so both the "usenet-shaped
    NONE fields" and "torrent-shaped NATIVE fields" halves of the vocabulary are exercised by
    the conformance suite via this one connector in stage 0.
    """

    family = "torrent"
    # `TORRENT_BASELINE` with two deliberate differences, so the conformance suite has both a
    # `Support.NONE` operation and a `Support.NONE` field to exercise against a real connector
    # instance, not just against the baseline constants directly (`test_clients_framework.py`'s
    # "field projection matches declared capabilities" check needs at least one `NONE` entry to
    # actually prove anything).
    capabilities = TORRENT_BASELINE.overridden(
        operations={
            Operation.RECHECK: Capability(Support.NONE, note="fake connector -- not modeled"),
        },
        fields={
            Field.UPLOADED_BYTES: Capability(Support.NONE, note="fake connector -- not modeled"),
        },
    )
    config_schema = (
        ConfigField(key="host", label="Host", kind="str"),
        ConfigField(key="api_key", label="API key", kind="secret", help_text="Fake, for tests."),
    )

    def __init__(self, *, config, state: FakeClientState | None = None) -> None:
        super().__init__(config=config)
        self.state = state if state is not None else FakeClientState()

    @staticmethod
    def map_phase(raw_status: str) -> TransferPhase:
        return _PHASE_MAP.get(raw_status, TransferPhase.UNKNOWN)

    def _check(self, op: Operation) -> None:
        if not self.state.reachable:
            raise ClientUnreachable(f"fake client unreachable for {op.value}")
        if op in self.state.fail_operations:
            raise CapabilityUnavailable(f"fake client cannot currently perform {op.value}")

    async def test_connection(self) -> ConnectionInfo:
        self._check(Operation.TEST_CONNECTION)
        return ConnectionInfo(version="1.0.0-fake")

    async def list_transfers(self, *, active_only: bool = False) -> list[Transfer]:
        self._check(Operation.LIST_TRANSFERS)
        transfers = self.state.transfers
        if active_only:
            terminal = (TransferPhase.COMPLETED, TransferPhase.FAILED)
            transfers = [t for t in transfers if t.phase not in terminal]
        return [project_transfer(t, self.capabilities) for t in transfers]

    async def list_history(self) -> list[Transfer]:
        self._check(Operation.LIST_HISTORY)
        terminal = (TransferPhase.COMPLETED, TransferPhase.FAILED)
        return [
            project_transfer(t, self.capabilities)
            for t in self.state.transfers
            if t.phase in terminal
        ]

    async def get_transfer(self, client_id: str) -> Transfer | None:
        self._check(Operation.GET_TRANSFER)
        for t in self.state.transfers:
            if t.client_id == client_id:
                return project_transfer(t, self.capabilities)
        return None

    async def list_trackers(self, client_id: str) -> list[TrackerInfo]:
        self._check(Operation.LIST_TRACKERS)
        return [TrackerInfo(host="tracker.example.test")]

    async def list_files(self, client_id: str) -> list[str]:
        self._check(Operation.LIST_FILES)
        return []

    async def list_base_paths(self) -> list[BasePath]:
        self._check(Operation.LIST_BASE_PATHS)
        return list(self.state.base_paths)

    async def free_space(self, path: str) -> SpaceInfo:
        self._check(Operation.FREE_SPACE)
        return SpaceInfo(free_bytes=self.state.free_bytes)

    async def pause(self, client_id: str) -> None:
        self._check(Operation.PAUSE)

    async def resume(self, client_id: str) -> None:
        self._check(Operation.RESUME)

    async def remove(self, client_id: str) -> RemoveOutcome:
        self._check(Operation.REMOVE)
        self.state.transfers = [t for t in self.state.transfers if t.client_id != client_id]
        return RemoveOutcome(succeeded=True)

    async def set_label(self, client_id: str, label: str) -> None:
        self._check(Operation.SET_LABEL)

    async def recheck(self, client_id: str) -> None:
        # This fake connector declares `Operation.RECHECK` unsupported (see `capabilities`
        # above) -- demonstrating the ABC's own suggested pattern (`DownloadClient.recheck`'s
        # docstring): raise `CapabilityUnavailable` immediately rather than silently no-op, so
        # a caller that skips its own `capabilities.supports(...)` check still gets a clear,
        # typed failure instead of quiet, meaningless success.
        raise CapabilityUnavailable("fake client does not support recheck")
