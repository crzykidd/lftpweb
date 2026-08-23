"""Conformance suite for the download-client connector framework
(docs/download-client-framework-spec.md §6.2) plus the direct unit tests for the framework
pieces that are not per-connector (spec §13.1).

Stage 0 (see `docs/decisions.md`) registers exactly one connector,
`fake_client.FakeDownloadClient`. The conformance tests below are written to run *unchanged*
against every future real adapter (SABnzbd, stage 1) the moment its module registers itself
with `core.clients.register_client` -- which is the entire point of a registry-parameterized
suite (spec §6.2: "at eight connectors this is the difference between an afternoon and a
week"). Two of the six conformance bullets spec §6.2 lists -- "only the three error types
escape its methods" and, end-to-end, "no field is declared that the connector cannot populate"
-- need a connector with test-controllable failure/data state to actually exercise, which only
`FakeDownloadClient` offers in stage 0; those are written as direct tests against it rather
than parameterized over the registry, and will only become genuinely registry-generic once a
stage-1 fake SABnzbd/fake rTorrent (spec §13.1) exists with its own controllable state. See the
stage-0 report for this noted as a scope boundary, not silently narrowed.
"""

from __future__ import annotations

from dataclasses import asdict

import pytest

# Importing names from `fake_client` runs its module body, which is what registers "fake" into
# the real registry via `@register_client("fake")` -- see that module's own docstring. No bare
# `import fake_client` is needed alongside this for the registration side effect to happen.
from fake_client import FakeClientState, FakeDownloadClient

from lftpweb.core.clients import (
    TORRENT_BASELINE,
    USENET_BASELINE,
    Capability,
    CapabilitySet,
    ConfigField,
    Field,
    Operation,
    Support,
    degrade_from_error,
    get_client_class,
    project_transfer,
    register_client,
    registered_clients,
)
from lftpweb.core.clients.errors import CapabilityUnavailable, ClientError, ClientUnreachable
from lftpweb.core.clients.models import Transfer, TransferPhase, normalize_client_id

REGISTERED_TYPES = sorted(registered_clients())


@pytest.fixture(params=REGISTERED_TYPES)
def client_class(request):
    return get_client_class(request.param)


def _fully_populated_transfer(client_id: str = "sample-1") -> Transfer:
    """One `Transfer` with every `Field`-governed attribute set to a non-`None` value --
    the fixture the projection conformance check needs to prove anything: a connector that
    never populates a field in the first place would trivially "pass" a check against an
    already-`None` sample.
    """
    return Transfer(
        client_id=client_id,
        name="Sample.Release.S01E01",
        phase=TransferPhase.DOWNLOADING,
        raw_status="downloading",
        raw={"id": client_id},
        content_path="/data/downloads/complete/Sample.Release.S01E01",
        size_bytes=1_000_000,
        bytes_done=500_000,
        eta_s=120,
        error_message=None,  # left None deliberately -- "no error" is a real, common state
        category="tv",
        added_at="2026-08-22T00:00:00Z",
        completed_at=None,
        ratio=1.5,
        uploaded_bytes=250_000,
        seed_time_s=3600,
        tracker_hosts=("tracker.example.test",),
    )


# ----------------------------------------------------------------------------------------
# Registry-parameterized conformance checks (spec §6.2) -- run once per registered connector.
# ----------------------------------------------------------------------------------------


def test_conformance_every_operation_and_field_is_declared(client_class):
    caps = client_class.capabilities
    assert set(caps.operations) == set(Operation), "an Operation key is missing or extra"
    assert set(caps.fields) == set(Field), "a Field key is missing or extra"


def test_conformance_config_schema_round_trips(client_class):
    for entry in client_class.config_schema:
        assert ConfigField(**asdict(entry)) == entry


def test_conformance_map_phase_is_total(client_class):
    # An input this connector's own map almost certainly never enumerated -- must still map to
    # a real `TransferPhase`, never raise.
    result = client_class.map_phase("a-status-string-no-real-client-has-ever-sent-xyz123")
    assert result is TransferPhase.UNKNOWN

    for weird in ("", "???", "12345", "DOWNLOADING", "🎉"):
        result = client_class.map_phase(weird)
        assert isinstance(result, TransferPhase), f"map_phase({weird!r}) did not return a phase"


def test_conformance_field_projection_matches_declared_capabilities(client_class):
    """spec §2.2: "a connector must never declare a field it cannot populate." Enforced
    structurally by `project_transfer` (`core.clients.base`) -- this asserts that function
    actually honors this connector's own declared capabilities: every field declared
    `Support.NONE` is nulled, and every field declared `NATIVE`/`DERIVED` survives untouched.
    """
    caps = client_class.capabilities
    sample = _fully_populated_transfer()
    projected = project_transfer(sample, caps)

    for member in Field:
        support = caps.fields[member].support
        value = getattr(projected, member.value)
        original = getattr(sample, member.value)
        if support is Support.NONE:
            assert value is None, f"{member} is declared NONE but survived projection as {value!r}"
        else:
            assert value == original, f"{member} is declared but project_transfer altered it"


# ----------------------------------------------------------------------------------------
# Direct tests against `FakeDownloadClient` -- state-driven behavior no other connector
# exists yet to parameterize over generically (see module docstring).
# ----------------------------------------------------------------------------------------


async def test_fake_list_transfers_masks_undeclared_field_end_to_end():
    """The end-to-end version of the projection check above: `FakeDownloadClient` declares
    `Field.UPLOADED_BYTES` as `Support.NONE` (see its own `capabilities` override) -- seeding a
    fully-populated `Transfer` and reading it back through `list_transfers()` must come back
    with `uploaded_bytes` nulled, proving the connector's own method actually calls
    `project_transfer` rather than merely being *capable* of it in the abstract.
    """
    state = FakeClientState(transfers=[_fully_populated_transfer()])
    client = FakeDownloadClient(config={}, state=state)

    (result,) = await client.list_transfers()

    assert result.uploaded_bytes is None
    assert result.ratio == 1.5  # a field this connector does declare survives untouched


async def test_fake_recheck_raises_capability_unavailable_for_declared_none_operation():
    client = FakeDownloadClient(config={})
    with pytest.raises(CapabilityUnavailable):
        await client.recheck("sample-1")


@pytest.mark.parametrize(
    "operation,call",
    [
        (Operation.TEST_CONNECTION, lambda c: c.test_connection()),
        (Operation.LIST_TRANSFERS, lambda c: c.list_transfers()),
        (Operation.PAUSE, lambda c: c.pause("id")),
        (Operation.REMOVE, lambda c: c.remove("id")),
    ],
)
async def test_only_three_error_types_escape_a_reachable_but_failing_call(operation, call):
    """spec §6.2: "only the three error types escape its methods." Forced via
    `FakeClientState.fail_operations`, the fake's own hook for "declares a capability and then
    fails it at runtime" (spec §4.1 layer 3).
    """
    state = FakeClientState(fail_operations={operation})
    client = FakeDownloadClient(config={}, state=state)
    with pytest.raises(CapabilityUnavailable) as exc_info:
        await call(client)
    assert type(exc_info.value) in (ClientUnreachable, ClientError, CapabilityUnavailable)


@pytest.mark.parametrize(
    "call",
    [
        lambda c: c.test_connection(),
        lambda c: c.list_transfers(),
        lambda c: c.pause("id"),
        lambda c: c.remove("id"),
    ],
)
async def test_only_three_error_types_escape_an_unreachable_client(call):
    state = FakeClientState(reachable=False)
    client = FakeDownloadClient(config={}, state=state)
    with pytest.raises(ClientUnreachable) as exc_info:
        await call(client)
    assert type(exc_info.value) in (ClientUnreachable, ClientError, CapabilityUnavailable)


# ----------------------------------------------------------------------------------------
# The three-layer merge -- "the single most important test in the stage" (handoff prompt).
# ----------------------------------------------------------------------------------------


def test_client_unreachable_never_degrades_a_capability():
    caps = CapabilitySet(
        operations={Operation.PAUSE: Capability(Support.NATIVE)},
        fields={},
    )
    result = degrade_from_error(caps, Operation.PAUSE, ClientUnreachable("no route to host"))
    assert result is caps  # completely unchanged -- not just equal, the identical object
    assert result.supports(Operation.PAUSE) is True


def test_bare_client_error_never_degrades_a_capability():
    caps = CapabilitySet(
        operations={Operation.PAUSE: Capability(Support.NATIVE)},
        fields={},
    )
    result = degrade_from_error(caps, Operation.PAUSE, ClientError("malformed response"))
    assert result is caps
    assert result.supports(Operation.PAUSE) is True


def test_capability_unavailable_does_degrade():
    caps = CapabilitySet(
        operations={Operation.PAUSE: Capability(Support.NATIVE)},
        fields={},
    )
    result = degrade_from_error(caps, Operation.PAUSE, CapabilityUnavailable("not supported"))
    assert result is not caps
    assert result.supports(Operation.PAUSE) is False
    # The original set is untouched -- `CapabilitySet` is immutable, so no caller holding a
    # reference to the pre-degrade set is surprised by a mutation underneath it.
    assert caps.supports(Operation.PAUSE) is True


def test_degrade_field_capability():
    caps = CapabilitySet(operations={}, fields={Field.RATIO: Capability(Support.NATIVE)})
    result = degrade_from_error(caps, Field.RATIO, CapabilityUnavailable("gone"))
    assert result.supports(Field.RATIO) is False


def test_narrowed_by_rejects_raising_support():
    caps = CapabilitySet(
        operations={Operation.PAUSE: Capability(Support.NONE)},
        fields={},
    )
    with pytest.raises(ValueError, match="narrow"):
        caps.narrowed_by(operations={Operation.PAUSE: Capability(Support.NATIVE)})


def test_narrowed_by_rejects_an_undeclared_key():
    caps = CapabilitySet(operations={}, fields={})
    with pytest.raises(KeyError):
        caps.narrowed_by(operations={Operation.PAUSE: Capability(Support.NONE)})


def test_narrowed_by_allows_equal_or_lower_rank():
    caps = CapabilitySet(
        operations={Operation.GET_TRANSFER: Capability(Support.DERIVED)},
        fields={},
    )
    same = caps.narrowed_by(operations={Operation.GET_TRANSFER: Capability(Support.DERIVED)})
    assert same.supports(Operation.GET_TRANSFER, accept_derived=True) is True
    lower = caps.narrowed_by(operations={Operation.GET_TRANSFER: Capability(Support.NONE)})
    assert lower.supports(Operation.GET_TRANSFER, accept_derived=True) is False


# ----------------------------------------------------------------------------------------
# `supports(..., accept_derived=...)` in both modes.
# ----------------------------------------------------------------------------------------


def test_supports_native_is_always_true_regardless_of_accept_derived():
    caps = CapabilitySet(operations={}, fields={Field.RATIO: Capability(Support.NATIVE)})
    assert caps.supports(Field.RATIO) is True
    assert caps.supports(Field.RATIO, accept_derived=True) is True


def test_supports_derived_needs_accept_derived():
    caps = CapabilitySet(operations={}, fields={Field.SEED_TIME_S: Capability(Support.DERIVED)})
    assert caps.supports(Field.SEED_TIME_S) is False
    assert caps.supports(Field.SEED_TIME_S, accept_derived=True) is True


def test_supports_none_is_always_false():
    caps = CapabilitySet(operations={}, fields={Field.SEED_TIME_S: Capability(Support.NONE)})
    assert caps.supports(Field.SEED_TIME_S) is False
    assert caps.supports(Field.SEED_TIME_S, accept_derived=True) is False


def test_supports_undeclared_key_is_false_not_a_raise():
    caps = CapabilitySet(operations={}, fields={})
    assert caps.supports(Field.RATIO) is False


# ----------------------------------------------------------------------------------------
# `normalize_client_id` (spec §7.1).
# ----------------------------------------------------------------------------------------


def test_normalize_client_id_lowercases_an_uppercase_infohash():
    # Real production evidence: docs/transfers-redesign-spec.md §4.4, an *arr `arr_matched`
    # event.
    uppercase = "12682AF0C00A061448BCFA16975A5D5F01A84A61"
    assert normalize_client_id(uppercase) == uppercase.lower()


def test_normalize_client_id_leaves_a_lowercase_infohash_unchanged():
    lowercase = "12682af0c00a061448bcfa16975a5d5f01a84a61"
    assert normalize_client_id(lowercase) == lowercase


def test_normalize_client_id_leaves_a_sab_nzo_id_untouched():
    # Real production evidence: docs/transfers-redesign-spec.md §4.4.
    nzo_id = "b67924d8-c0f0-4901-8941-85ddbfef6179"
    assert normalize_client_id(nzo_id) == nzo_id


# ----------------------------------------------------------------------------------------
# Baseline profile override semantics (spec §5).
# ----------------------------------------------------------------------------------------


def test_torrent_baseline_differs_from_usenet_baseline_exactly_as_specced():
    # spec §5: "the above plus ratio, uploaded bytes, seed time, trackers, recheck."
    for field_member in (Field.RATIO, Field.UPLOADED_BYTES, Field.SEED_TIME_S, Field.TRACKER_HOSTS):
        assert USENET_BASELINE.fields[field_member].support is Support.NONE
        assert TORRENT_BASELINE.fields[field_member].support is Support.NATIVE

    assert USENET_BASELINE.operations[Operation.RECHECK].support is Support.NONE
    assert TORRENT_BASELINE.operations[Operation.RECHECK].support is Support.NATIVE
    assert USENET_BASELINE.operations[Operation.LIST_TRACKERS].support is Support.NONE
    assert TORRENT_BASELINE.operations[Operation.LIST_TRACKERS].support is Support.NATIVE

    # Both baselines fully declare the vocabulary -- the property that lets a connector
    # override nothing at all and still pass the "every key declared" conformance check.
    assert set(USENET_BASELINE.operations) == set(Operation)
    assert set(USENET_BASELINE.fields) == set(Field)
    assert set(TORRENT_BASELINE.operations) == set(Operation)
    assert set(TORRENT_BASELINE.fields) == set(Field)


def test_both_baselines_declare_list_categories_native():
    # spec §5, §2.1 (LIST_CATEGORIES joined the vocabulary 2026-08-23): both baselines start
    # from "the client can enumerate its own categories" -- a connector without a real, closed
    # category list (rTorrent) is expected to override this down itself, the same way it
    # overrides `Field.SEED_TIME_S` down from the baseline's own NATIVE claim (§13.6 #5).
    assert USENET_BASELINE.operations[Operation.LIST_CATEGORIES].support is Support.NATIVE
    assert TORRENT_BASELINE.operations[Operation.LIST_CATEGORIES].support is Support.NATIVE


def test_overridden_can_raise_support_freely_unlike_narrowed_by():
    # `.overridden` is the authoring-time tool (spec §5) -- unlike `.narrowed_by`, it may raise
    # a key's support, which is exactly what a connector declaring "actually, I have this one
    # native, unlike my baseline" needs to do.
    custom = USENET_BASELINE.overridden(fields={Field.RATIO: Capability(Support.NATIVE)})
    assert custom.supports(Field.RATIO) is True
    assert USENET_BASELINE.supports(Field.RATIO) is False  # the original is untouched


# ----------------------------------------------------------------------------------------
# Registry.
# ----------------------------------------------------------------------------------------


def test_duplicate_registration_raises():
    @register_client("__test_dup__")
    class _First(FakeDownloadClient):
        pass

    with pytest.raises(ValueError, match="__test_dup__"):

        @register_client("__test_dup__")
        class _Second(FakeDownloadClient):
            pass


def test_get_client_class_returns_the_registered_class():
    assert get_client_class("fake") is FakeDownloadClient


def test_get_client_class_unknown_type_raises_key_error():
    with pytest.raises(KeyError):
        get_client_class("__does_not_exist__")
