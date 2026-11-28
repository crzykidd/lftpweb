"""Host allow-list and WebSocket Origin matching (audit batch C3, DESIGN.md §8).

Pure functions; `middleware.py.RequestGuardMiddleware` is the ASGI glue that calls them.

**Why a Host allow-list at all.** DNS rebinding: a website a LAN user visits can re-point its
own hostname at the lftpweb instance, and the browser then treats lftpweb as *that site* --
same origin, so no CORS check, no custom-header preflight, nothing a cross-site defence can
see. The one thing the attacker cannot change is the `Host` header: the browser sends the
attacker's name. Refusing unknown Host values is the defence, and the only one.

**Why the default is permissive.** Nothing in the request tells lftpweb which names a user
reaches it by (a LAN IP, a `.home.arpa` name, a reverse-proxy hostname, a Tailscale name), so
an enforced default would lock out most existing installs on upgrade. Unset therefore means
"accept any Host, as before", with a startup WARNING and a UI banner in `none`/`proxy` mode
(the user's decision, 2026-11-26; `docs/decisions.md`).

**Matching rules** (`LFTPWEB_ALLOWED_HOSTS`, comma-separated, case-insensitive):

* `name` or `1.2.3.4` or `[::1]` -- that host on any port.
* `name:8087` / `[fd00::1]:8087` -- that host on that port only.
* `*.example.com` -- any subdomain of `example.com` (any depth), not `example.com` itself.
  Only a leading `*.` is a wildcard; a `*` anywhere else makes the entry invalid.
* `localhost`, `127.0.0.1` and `::1` are always allowed, on any port, whatever the list says.
  A browser on an attacker's page sends the attacker's name as Host, never these, so allowing
  them costs nothing against rebinding and keeps the container HEALTHCHECK (`curl
  http://127.0.0.1:8087/api/health`) and a local recovery path working.

An invalid entry is logged at startup and ignored -- never fatal, since a crash-looping
container is a worse lock-out than a 403 that names the env var.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from functools import lru_cache
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

ENV_VAR = "LFTPWEB_ALLOWED_HOSTS"

LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})

_DEFAULT_PORTS = {"http": 80, "ws": 80, "https": 443, "wss": 443}

# A hostname label set / IPv4 (no brackets); IPv6 is handled separately via `[...]`.
_NAME_RE = re.compile(r"[a-z0-9_]([a-z0-9_.-]*[a-z0-9_])?")
_IPV6_RE = re.compile(r"[0-9a-f:.]+(%[a-z0-9_.-]+)?")


@dataclass(frozen=True)
class HostRule:
    host: str  # lowercased; IPv6 without brackets; for a wildcard, the suffix after `*.`
    port: int | None  # None = any port
    wildcard: bool = False

    def matches(self, host: str, port: int | None) -> bool:
        if self.port is not None and port != self.port:
            return False
        if self.wildcard:
            return host.endswith("." + self.host)
        return host == self.host


def split_host_port(value: str) -> tuple[str, int | None] | None:
    """`"Name:8087"` -> `("name", 8087)`, `"[::1]:80"` -> `("::1", 80)`, `"x"` -> `("x",
    None)`. A trailing dot on a name is dropped (`example.com.` is `example.com`). `None` if
    the value can't be parsed (bad port, unbalanced bracket, empty).
    """
    value = value.strip().lower()
    if not value:
        return None
    port_text: str | None = None
    if value.startswith("["):
        end = value.find("]")
        if end == -1:
            return None
        host = value[1:end]
        rest = value[end + 1 :]
        if rest:
            if not rest.startswith(":"):
                return None
            port_text = rest[1:]
        if not _IPV6_RE.fullmatch(host):
            return None
    elif value.count(":") == 1:
        host, port_text = value.split(":", 1)
    elif ":" in value:
        # A bare IPv6 literal with no brackets (not valid in a Host header, but harmless to
        # accept in the env var): no port.
        host = value
        if not _IPV6_RE.fullmatch(host):
            return None
    else:
        host = value
    host = host.rstrip(".") if ":" not in host else host
    if not host:
        return None
    port: int | None = None
    if port_text is not None:
        if not port_text.isdigit() or not 0 < int(port_text) < 65536:
            return None
        port = int(port_text)
    return host, port


def _parse_entry(entry: str) -> HostRule | None:
    wildcard = False
    text = entry.strip().lower()
    if text.startswith("*."):
        wildcard = True
        text = text[2:]
    if "*" in text:
        return None
    parsed = split_host_port(text)
    if parsed is None:
        return None
    host, port = parsed
    if ":" not in host and not _NAME_RE.fullmatch(host):
        return None
    if wildcard and ":" in host:
        return None
    return HostRule(host=host, port=port, wildcard=wildcard)


@dataclass(frozen=True)
class AllowedHosts:
    """The parsed env var. `enforced` is False only when the env var is unset or blank."""

    enforced: bool
    rules: tuple[HostRule, ...]
    invalid: tuple[str, ...]

    def allows(self, host_header: str | None) -> bool:
        if not self.enforced:
            return True
        if not host_header:
            return False
        parsed = split_host_port(host_header)
        if parsed is None:
            return False
        host, port = parsed
        if host in LOOPBACK_HOSTS:
            return True
        return any(rule.matches(host, port) for rule in self.rules)


@lru_cache(maxsize=8)
def parse_allowed_hosts(raw: str | None) -> AllowedHosts:
    """Cached on the raw string: the middleware calls this per request with the current
    `config.settings.allowed_hosts`, which tests monkeypatch.
    """
    if raw is None or not raw.strip():
        return AllowedHosts(enforced=False, rules=(), invalid=())
    rules: list[HostRule] = []
    invalid: list[str] = []
    for entry in raw.split(","):
        if not entry.strip():
            continue
        rule = _parse_entry(entry)
        if rule is None:
            invalid.append(entry.strip())
        else:
            rules.append(rule)
    return AllowedHosts(enforced=True, rules=tuple(rules), invalid=tuple(invalid))


def log_startup_posture(raw: str | None, mode: str) -> None:
    """One log line at startup describing DNS-rebinding protection (`main.py.lifespan`)."""
    allowed = parse_allowed_hosts(raw)
    for entry in allowed.invalid:
        logger.error("%s: ignoring invalid entry %r", ENV_VAR, entry)
    if allowed.enforced:
        logger.info(
            "%s is set: requests are accepted only for %d listed host(s) plus loopback",
            ENV_VAR,
            len(allowed.rules),
        )
    elif mode in ("none", "proxy"):
        logger.warning(
            "DNS-rebinding protection is off: %s is not set, so a request with any Host "
            "header is accepted (auth mode %r). Set %s to the name(s) you reach lftpweb by, "
            "e.g. %s=lftpweb.example.com,192.168.1.10 -- see README 'Network exposure'.",
            ENV_VAR,
            mode,
            ENV_VAR,
            ENV_VAR,
        )


def origin_allowed(
    origin: str,
    host_header: str | None,
    forwarded_host: str | None,
    allowed: AllowedHosts,
) -> bool:
    """A WebSocket handshake's `Origin` must name the host the request was made to.

    Matches when the Origin's host and port equal the request's `Host` (default ports by the
    Origin's scheme), **or** its `X-Forwarded-Host` (a reverse proxy, or the Vite dev proxy,
    that rewrites `Host` to its upstream name), **or** an entry in `LFTPWEB_ALLOWED_HOSTS`.
    Accepting `X-Forwarded-Host` here is safe only because this check is for the WebSocket
    handshake, where a browser page cannot add request headers; it is never used for the Host
    allow-list itself, where a same-origin (rebound) `fetch` could set it.
    """
    try:
        parts = urlsplit(origin.strip())
        origin_port = parts.port
    except ValueError:
        return False
    scheme = parts.scheme.lower()
    if scheme not in _DEFAULT_PORTS or not parts.hostname:
        return False  # includes the literal `null` Origin
    origin_host = parts.hostname.lower().rstrip(".")
    effective_origin_port = origin_port or _DEFAULT_PORTS[scheme]

    for candidate in (host_header, forwarded_host.split(",")[0] if forwarded_host else None):
        if not candidate:
            continue
        parsed = split_host_port(candidate)
        if parsed is None:
            continue
        host, port = parsed
        if host == origin_host and (port or _DEFAULT_PORTS[scheme]) == effective_origin_port:
            return True

    if allowed.enforced:
        return any(rule.matches(origin_host, effective_origin_port) for rule in allowed.rules)
    return False


# --- Trusted reverse proxies (audit batch C5, F9/F10) ---------------------------------------

TRUSTED_PROXIES_ENV_VAR = "LFTPWEB_TRUSTED_PROXIES"


def parse_trusted_proxies(raw: str | None) -> list[str] | str:
    """Parse `LFTPWEB_TRUSTED_PROXIES` into what uvicorn's `ProxyHeadersMiddleware` accepts: the
    string `"*"` (trust every peer) or a list of IP / CIDR strings (empty = trust nobody).

    An invalid entry is logged and ignored -- never fatal, and never widening: dropping an entry
    can only make lftpweb trust *fewer* peers, which is the safe direction.
    """
    import ipaddress

    entries = [e.strip() for e in (raw or "").split(",") if e.strip()]
    if "*" in entries:
        return "*"
    valid: list[str] = []
    for entry in entries:
        try:
            if "/" in entry:
                ipaddress.ip_network(entry, strict=False)
            else:
                ipaddress.ip_address(entry)
        except ValueError:
            logger.warning("%s: ignoring invalid entry %r", TRUSTED_PROXIES_ENV_VAR, entry)
            continue
        valid.append(entry)
    return valid


def log_trusted_proxies_posture(raw: str | None) -> None:
    """One startup line saying whether forwarded headers are honoured, and a WARNING for `*`."""
    parsed = parse_trusted_proxies(raw)
    if parsed == "*":
        logger.warning(
            "%s=* : X-Forwarded-For / X-Forwarded-Proto are trusted from ANY peer. Anyone who can "
            "reach lftpweb directly can forge their client IP (evading the login rate limiter) "
            "and the cookie scheme. List your proxy's address instead.",
            TRUSTED_PROXIES_ENV_VAR,
        )
    elif parsed:
        logger.info("%s: forwarded headers trusted from %s", TRUSTED_PROXIES_ENV_VAR, parsed)
