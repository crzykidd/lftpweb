"""The download-client connector registry (docs/download-client-framework-spec.md §6) -- one
module-level dict keyed by `client_type`, populated by a decorator, with every adapter module
imported explicitly right here.

**No entry-points, no dynamic import scanning.** This project ships one Docker image
(`CLAUDE.md`), so there is nothing to discover at runtime that isn't already known at build
time -- adding a connector is one file plus one import line below (spec §6), not a packaging
concern. `core/clients/` is the first subpackage under `core/`, which is otherwise flat --
deliberate at the 7-10 adapter modules plus four framework modules this is expected to grow
into (spec §6); a flat `core/` would be unreadable at that size.

Stage 0 (this module's first version) registers no real adapter -- SABnzbd is stage 1 (spec
§14). `tests/fake_client.py` registers a `"fake"` connector for the conformance suite and the
capability-merge unit tests only; it is never imported here, so it never reaches the
production registry a real deployment sees.
"""

from __future__ import annotations

from types import MappingProxyType

from .base import (
    Capability,
    CapabilitySet,
    ConfigField,
    DownloadClient,
    Field,
    Operation,
    Support,
    TORRENT_BASELINE,
    USENET_BASELINE,
    degrade_from_error,
    project_transfer,
)

_registry: dict[str, type[DownloadClient]] = {}


def register_client(client_type: str):
    """Class decorator: register a `DownloadClient` subclass under `client_type`.

    Registering a duplicate `client_type` is a `ValueError`, not a silent overwrite (spec §6) --
    two connector modules racing to claim the same key is an authoring mistake that must be
    caught at import time, never a runtime choice about which one silently wins.
    """

    def _decorate(cls: type[DownloadClient]) -> type[DownloadClient]:
        if client_type in _registry:
            existing = _registry[client_type].__qualname__
            raise ValueError(
                f"client_type {client_type!r} is already registered to {existing} -- "
                f"cannot also register {cls.__qualname__}"
            )
        cls.client_type = client_type
        _registry[client_type] = cls
        return cls

    return _decorate


def get_client_class(client_type: str) -> type[DownloadClient]:
    """Look up a registered connector class by its `client_type` key. Raises `KeyError` for an
    unregistered type -- the caller (settings/instance CRUD, stage 1+) decides how to surface
    that as a user-facing error; this module only ever knows about the registry itself.
    """
    return _registry[client_type]


def registered_clients() -> MappingProxyType[str, type[DownloadClient]]:
    """A read-only view of the registry -- the conformance suite's own iteration point
    (spec §6.2: "a test parameterized over the whole registry").
    """
    return MappingProxyType(_registry)


__all__ = [
    "Capability",
    "CapabilitySet",
    "ConfigField",
    "DownloadClient",
    "Field",
    "Operation",
    "Support",
    "TORRENT_BASELINE",
    "USENET_BASELINE",
    "degrade_from_error",
    "get_client_class",
    "project_transfer",
    "register_client",
    "registered_clients",
]

# Explicit adapter imports go here, one per connector, each triggering its own
# `@register_client(...)` decorator on import (spec §6: "adding a connector is one file plus
# one import line"). Stage 0 ships none; SABnzbd (spec §14 stage 1) is the first.
