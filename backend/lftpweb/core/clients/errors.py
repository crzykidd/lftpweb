"""The three-way error taxonomy every download-client connector method must raise through
(docs/download-client-framework-spec.md §4.2) -- deliberately *not* collapsed into one
exception type the way `core/arrclient.py.ArrClientError` is, because here the distinction
changes what a caller does next, not just how it logs.

`core/arrclient.py.ArrClientError` folds DNS failure, a 500, and a timeout into one class on
purpose: `core/arrsync.py`'s poller treats all of them identically (log once, write one event
row, back off), so drawing the distinction there would be extra plumbing with no caller that
reads it. Here the distinction is load-bearing: only `CapabilityUnavailable` may ever degrade a
declared capability (spec §4.1 layer 3, enforced structurally by
`core.clients.base.degrade_from_error`). A caller that gets a `ClientUnreachable` or a bare
`ClientError` must change no capability, ever -- collapsing those with `CapabilityUnavailable`
would let one bad network minute permanently disable a feature the client actually supports,
which is exactly the shape of the v0.2.4 SABnzbd blank-queue incident
(docs/download-client-framework-spec.md §1, "§4.2 is not hypothetical") recurring one layer up,
against capabilities instead of against `item.state`.

`ClientError` is the base of the taxonomy, not a fourth, broader thing sitting above it --
raised directly for "this call failed and neither of the other two reasons applies" (a
malformed response body, an unexpected status code that isn't a capability signal). The
conformance suite (`tests/test_clients_framework.py`) asserts that only these three concrete
types ever escape a connector's methods.
"""

from __future__ import annotations


class ClientError(Exception):
    """Base of the taxonomy: a call to a download client did not succeed.

    Raised directly when a call failed for a reason that is neither "could not reach the
    client at all" (`ClientUnreachable`) nor "the client explicitly cannot do this"
    (`CapabilityUnavailable`) -- e.g. a malformed response body, or a non-2xx status the
    connector cannot otherwise classify. Surfaced to the caller; changes no capability
    (spec §4.2).
    """


class ClientUnreachable(ClientError):
    """Could not talk to the client at all -- DNS failure, connection refused, TLS failure,
    timeout.

    Says nothing about what the client *supports*, only that this attempt to reach it did not
    land. A caller's response is `docs/transfers-redesign-spec.md` §4.8's existing shape
    (spec §9): keep the last known state, back off, and change no capability. Never raised as
    a substitute for `CapabilityUnavailable` merely because both are "the client didn't do
    what I wanted" -- the two must stay distinguishable at the type level for
    `degrade_from_error` (`core.clients.base`) to do its job.
    """


class CapabilityUnavailable(ClientError):
    """The client explicitly said it cannot do this, or a post-condition proved that it
    didn't -- e.g. a pre-5.0 qBittorrent API rejecting a renamed operation, or a plugin this
    deployment does not have installed.

    This is the **only** member of the taxonomy a caller may use to degrade a declared
    capability (spec §4.1 layer 3) and write the accompanying audit event. Raising this for
    anything short of an explicit "unsupported" signal (a transport failure, a timeout) is the
    one mistake this taxonomy exists to make structurally awkward: reach for
    `ClientUnreachable` or the base `ClientError` first, and only raise this when the client
    itself said no.
    """
