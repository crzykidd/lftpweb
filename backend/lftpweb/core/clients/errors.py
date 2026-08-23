"""The error taxonomy every download-client connector method must raise through
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
conformance suite (`tests/test_clients_framework.py`) asserts that only `ClientUnreachable`,
`ClientError` itself, and `CapabilityUnavailable` escape `FakeDownloadClient`'s methods -- the
three concrete leaves stage 0 needed. A real connector may narrow `ClientError` further (e.g.
`ClientAuthenticationFailed`, below) when a fact is specific enough to deserve its own type and
its own message; every such subclass still answers `isinstance(exc, ClientError)` and still
changes no capability unless it is also a `CapabilityUnavailable`, so the load-bearing rule --
"only `CapabilityUnavailable` degrades" -- holds regardless of how many leaves the base class
grows.
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


class ClientAuthenticationFailed(ClientError):
    """The client reached and understood the request, then rejected the configured credential.

    Added 2026-08-22 against **measured** SABnzbd 5.1.1 behaviour
    (docs/download-client-framework-spec.md §13.4 #9, GitHub #23): an authenticated call
    (`mode=queue`/`history`/`get_config`) answers a bad API key with **HTTP 403,
    `text/html`, plain-text body `"API Key Incorrect"`** -- not the `{"status": false, "error":
    ...}` JSON envelope on a 200 the connector used to assume. "Wrong credential" and "host
    unreachable" are different facts a user acts on differently (fix the key vs. check the
    host/port), so they get different messages rather than both flattening to the base
    `ClientError`'s generic "the call failed."

    **Subclasses `ClientError`, not `ClientUnreachable`.** The two questions are orthogonal --
    SABnzbd answered the request just fine, it simply refused the key -- and
    `ClientUnreachable`'s contract (spec §9: keep the last known state, back off, retry) is
    built for a transient network condition. A wrong API key is not transient; retrying the
    exact same request will not fix it, so folding it into `ClientUnreachable` would teach a
    poller to quietly back off from a problem backing off cannot solve.

    **Deliberately *not* `CapabilityUnavailable`.** `CapabilityUnavailable`'s own docstring
    reserves it for the client explicitly saying "I cannot do this" (spec §4.1 layer 3) -- the
    one signal `degrade_from_error` (`core.clients.base`) is allowed to act on. A bad API key
    says nothing about what the client *supports*; it only says lftpweb is not currently
    allowed to ask. Raising `CapabilityUnavailable` here would let `degrade_from_error` silently
    disable a capability the client genuinely has, over one wrong credential -- exactly the
    v0.2.4 SABnzbd blank-queue shape this taxonomy exists to keep out (spec §4.2, and this
    class's own sibling `ClientUnreachable`'s docstring), now one layer closer since the trigger
    would be a config typo rather than a network blip.
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
