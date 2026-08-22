"""The redacted response-capture helper (docs/download-client-framework-spec.md §13.3) --
turns a raw HTTP exchange into something safe to write to a log line, *before* anything is
written, never "later, before display."

**Why this exists at all**: SABnzbd's auth is an `apikey` query parameter (spec §13.3, this
stage's own SABnzbd connector), so the secret sits in the request *URL* -- any naive
`logger.debug("GET %s", url)` leaks it. The same helper is written generic enough for the
rTorrent connector (stage 2+) to reuse for a different secret shape: an announce URL embeds a
per-user passkey (spec §7.3), and `redact_announce_url` below reduces one to its host only,
matching `models.TrackerInfo`'s own "hostname only, never the full announce URL" rule.

Two independent redaction shapes, because the two secrets are shaped differently:

- `redact_secret` -- a **known literal value** (an API key whose plaintext the caller already
  holds) that may appear anywhere in a captured string: in a query string, in a JSON body, or
  more than once. A plain substring replace is the only fully reliable way to redact this,
  since the value is known exactly rather than pattern-matched.
- `redact_announce_url` -- the caller does **not** hold the secret as a standalone string; it
  is embedded in a URL's query string under a tracker-specific key name. Reducing the whole URL
  to `scheme://host[:port]` sidesteps needing to know the passkey's parameter name at all.

`capture_response` composes both concerns applications actually need at a capture site:
redact every known secret, then cap the sample size -- in that order, deliberately, so a
secret that would otherwise straddle the truncation boundary is fully collapsed to a short,
fixed-width marker *before* the length check ever runs, and no partial secret can survive
truncation.
"""

from __future__ import annotations

from urllib.parse import urlsplit

# A capped sample is meant to answer "is this client reachable, and what did it say" from a
# log line -- not to stand in for a full response body. `core/supportbundle.py`'s per-*arr-log
# fetch budget (20 MiB) caps a *whole downloaded file*, a completely different scale; this caps
# one line-oriented diagnostic sample, so a much smaller number is the right one. 4 KiB is
# comfortably enough to see a full SABnzbd `mode=version`/error body or the shape of a
# truncated `mode=queue` response, while keeping a single capture from ever becoming a second
# copy of the log itself.
DEFAULT_CAPTURE_BYTE_CAP = 4096

_REDACTED_MARKER = "***REDACTED***"


def redact_secret(text: str, *, secret: str, replacement: str = _REDACTED_MARKER) -> str:
    """Replace every occurrence of `secret` in `text` with `replacement`.

    A plain `str.replace` on purpose: `secret`'s exact literal value is known at the call site
    (an API key the connector already holds in plaintext), never inferred by a regex heuristic,
    so a straightforward substring replace is both correct and the cheapest way to catch every
    occurrence -- in a query string (`?apikey=...`), in a JSON body value, or repeated more than
    once in the same string (a URL logged alongside a body that also echoes it back). An empty
    `secret` is a no-op rather than replacing every character boundary in `text`, which
    `"".join(...)`-style accidents could otherwise produce.
    """
    if not secret:
        return text
    return text.replace(secret, replacement)


def redact_announce_url(url: str) -> str:
    """Reduce a full BitTorrent announce URL down to `scheme://host[:port]` (spec §7.3) --
    announce URLs embed a per-user passkey in the path or query string, and the passkey's
    parameter name and position vary by tracker software, so there is no single literal value
    to hand `redact_secret`. Keeping only the network location is what `models.TrackerInfo`
    already commits to for a parsed tracker record; this is the same rule applied to a raw
    string a capture is about to write to a log.

    Tolerant of a malformed or non-URL string: returns it **unchanged** rather than raising --
    a capture helper must never itself crash the call site that invokes it, and an unparseable
    string was never going to leak a passkey through a URL parser it doesn't fit ends up
    "redacted" trivially, by having nothing to redact.
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    if not parts.scheme or not parts.netloc:
        return url
    return f"{parts.scheme}://{parts.netloc}"


def cap_sample(text: str, *, max_bytes: int = DEFAULT_CAPTURE_BYTE_CAP) -> str:
    """Truncate `text` to at most `max_bytes` (UTF-8 encoded), the way
    `core/supportbundle.py`'s `ARR_LOG_PER_FILE_BYTE_CAP` caps a single fetched *arr log file --
    a safety cap against an unbounded or pathological response, not a number tuned against one
    real observed size. Truncation happens on an encoded byte boundary and decodes the
    remainder tolerantly (`errors="ignore"`), so a multi-byte UTF-8 character split by the cut
    is dropped rather than raising or emitting a corrupt half-character.
    """
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= max_bytes:
        return text
    truncated = encoded[:max_bytes].decode("utf-8", errors="ignore")
    return f"{truncated}...<truncated at {max_bytes} bytes>"


def capture_response(
    raw: str,
    *,
    secrets: tuple[str, ...] = (),
    max_bytes: int = DEFAULT_CAPTURE_BYTE_CAP,
) -> str:
    """The one call site a connector actually uses (spec §13.3): redact every secret in
    `secrets`, then cap the result -- in that order (see module docstring for why the order is
    load-bearing). `raw` is typically the request URL plus the response body concatenated, so a
    secret riding the URL (SABnzbd's `apikey` query parameter) is caught even though it never
    appears in the response body itself.
    """
    redacted = raw
    for secret in secrets:
        redacted = redact_secret(redacted, secret=secret)
    return cap_sample(redacted, max_bytes=max_bytes)
