"""`core.clients.detection` (docs/download-client-framework-spec.md §8.2 correction, migration
028; prompts/2026-08-22-client-base-paths-detected.md) -- fully testable locally, per spec
§13.1: `report_base_paths` only needs a `DownloadClient`-shaped stub and a `CapabilitySet`;
`verify_reported_paths` only needs something that duck-types `asyncssh.SFTPClient.stat`
(`tests/test_browse.py`'s own `_FakeSFTP` pattern, reduced here to just the one method
`core.browse.remote_directory_error` actually calls). No live SSH connection, no real
connector, and no API layer needed for any of this.
"""

from __future__ import annotations

import asyncssh

from lftpweb.core.clients import Capability, Operation, Support, USENET_BASELINE
from lftpweb.core.clients.detection import (
    BasePathState,
    report_base_paths,
    report_categories,
    verify_reported_paths,
)
from lftpweb.core.clients.errors import ClientError
from lftpweb.core.clients.models import BasePath, BasePathKind

# --- report_base_paths -----------------------------------------------------------------------


class _StubClient:
    """The one method `report_base_paths`/`report_categories` calls -- a bare stand-in, not a
    full `DownloadClient` (nothing else in the ABC is needed to exercise detection in isolation).
    """

    def __init__(
        self,
        *,
        paths: list[BasePath] | None = None,
        categories: list[str] | None = None,
        raises: Exception | None = None,
    ):
        self._paths = paths or []
        self._categories = categories or []
        self._raises = raises
        self.calls = 0

    async def list_base_paths(self) -> list[BasePath]:
        self.calls += 1
        if self._raises is not None:
            raise self._raises
        return list(self._paths)

    async def list_categories(self) -> list[str]:
        self.calls += 1
        if self._raises is not None:
            raise self._raises
        return list(self._categories)


async def test_report_base_paths_returns_the_connectors_own_answer():
    client = _StubClient(paths=[BasePath(path="/downloads/complete", kind=BasePathKind.CONTENT)])
    result = await report_base_paths(client, USENET_BASELINE)
    assert result == [BasePath(path="/downloads/complete", kind=BasePathKind.CONTENT)]


async def test_report_base_paths_never_calls_a_connector_that_does_not_declare_it():
    """A connector that does not declare `Operation.LIST_BASE_PATHS` (accepting derived) simply
    detects nothing -- **not an error**, and this asserts the stronger claim: the connector's
    own `list_base_paths` is never even called, so a torrent-only stub that hasn't implemented
    it can't accidentally blow up here either.
    """
    caps = USENET_BASELINE.overridden(
        operations={Operation.LIST_BASE_PATHS: Capability(Support.NONE)}
    )
    client = _StubClient(paths=[BasePath(path="/x", kind=BasePathKind.CONTENT)])
    result = await report_base_paths(client, caps)
    assert result == []
    assert client.calls == 0


async def test_report_base_paths_swallows_a_client_error_without_failing_the_connection_test():
    client = _StubClient(raises=ClientError("boom"))
    result = await report_base_paths(client, USENET_BASELINE)
    assert result == []


async def test_report_base_paths_swallows_a_not_implemented_connector_too():
    # Broader than the declared `ClientError` taxonomy on purpose -- a connector that hasn't
    # wired up `list_base_paths` yet (a test double, an in-progress adapter) must be exactly as
    # harmless as one that raises `ClientError` for real (this module's own docstring).
    client = _StubClient(raises=NotImplementedError("not wired up yet"))
    result = await report_base_paths(client, USENET_BASELINE)
    assert result == []


# --- report_categories (spec §8.3, joined 2026-08-23) --------------------------------------


async def test_report_categories_returns_the_connectors_own_answer():
    client = _StubClient(categories=["movies", "tv"])
    result = await report_categories(client, USENET_BASELINE)
    assert result == ["movies", "tv"]


async def test_report_categories_never_calls_a_connector_that_does_not_declare_it():
    caps = USENET_BASELINE.overridden(
        operations={Operation.LIST_CATEGORIES: Capability(Support.NONE)}
    )
    client = _StubClient(categories=["movies"])
    result = await report_categories(client, caps)
    assert result == []
    assert client.calls == 0


async def test_report_categories_accepts_a_derived_declaration():
    caps = USENET_BASELINE.overridden(
        operations={Operation.LIST_CATEGORIES: Capability(Support.DERIVED, note="labels in use")}
    )
    client = _StubClient(categories=["tv"])
    result = await report_categories(client, caps)
    assert result == ["tv"]


async def test_report_categories_swallows_a_client_error_without_failing_the_connection_test():
    client = _StubClient(raises=ClientError("boom"))
    result = await report_categories(client, USENET_BASELINE)
    assert result == []


async def test_report_categories_swallows_a_not_implemented_connector_too():
    client = _StubClient(raises=NotImplementedError("not wired up yet"))
    result = await report_categories(client, USENET_BASELINE)
    assert result == []


# --- verify_reported_paths --------------------------------------------------------------------


class _FakeAttrs:
    def __init__(self, type: int) -> None:
        self.type = type


class _FakeStatSFTP:
    """Duck-types just `stat` -- the one method `core.browse.remote_directory_error` calls.
    `existing` is the set of paths this fake reports as real directories; anything else reports
    missing, the same "clean not-found" shape a real seedbox gives for a genuinely absent path.
    """

    def __init__(self, existing: set[str]) -> None:
        self._existing = existing

    async def stat(self, path: str) -> _FakeAttrs:
        if path in self._existing:
            return _FakeAttrs(type=asyncssh.FILEXFER_TYPE_DIRECTORY)
        raise asyncssh.SFTPNoSuchFile(f"no such file: {path}")


class _DenyingSFTP:
    """An ambiguous failure -- permission denied stat'ing an otherwise-real path -- the shape
    `core.browse.remote_directory_error`'s own docstring says must propagate as itself, never
    collapse into "not found."
    """

    async def stat(self, path: str) -> _FakeAttrs:
        raise asyncssh.SFTPPermissionDenied("nope")


class _FakeTildeSFTP:
    """Also duck-types `realpath` -- the method `_resolve_tilde_candidate` (finding #1,
    2026-08-23, prompts/2026-08-23-tilde-and-visibility.md) calls to expand a `~`/relative
    `client_path` against the SSH home, the same primitive `core.browse.resolve_remote_dir`
    already uses for the path-browse dialog. `realpath_map` is this fake server's whole
    "home directory" -- a path not in it raises, the same "an unusual server refused" shape
    `resolve_remote_dir`'s own fallback branch already handles.
    """

    def __init__(self, existing: set[str], realpath_map: dict[str, str] | None = None) -> None:
        self._existing = existing
        self._realpath_map = realpath_map or {}

    async def stat(self, path: str) -> _FakeAttrs:
        if path in self._existing:
            return _FakeAttrs(type=asyncssh.FILEXFER_TYPE_DIRECTORY)
        raise asyncssh.SFTPNoSuchFile(f"no such file: {path}")

    async def realpath(self, path: str) -> str:
        if path in self._realpath_map:
            return self._realpath_map[path]
        raise asyncssh.SFTPNoSuchPath(f"cannot resolve: {path}")


async def test_verify_reported_paths_marks_an_existing_path_verified():
    reported = [BasePath(path="/data/pickup", kind=BasePathKind.CONTENT)]
    result = await verify_reported_paths(reported, _FakeStatSFTP(existing={"/data/pickup"}))
    assert len(result) == 1
    assert result[0].client_path == "/data/pickup"
    assert result[0].kind == BasePathKind.CONTENT
    assert result[0].state == BasePathState.VERIFIED


async def test_verify_reported_paths_marks_a_missing_path_not_found():
    reported = [BasePath(path="/complete", kind=BasePathKind.CONTENT)]
    result = await verify_reported_paths(reported, _FakeStatSFTP(existing=set()))
    assert result[0].state == BasePathState.NOT_FOUND


async def test_verify_reported_paths_marks_an_ambiguous_failure_unverified_not_not_found():
    """The distinction this task's own handoff prompt calls out as the one most likely to be
    got wrong: a permission error is not the same fact as the seedbox cleanly reporting the
    path missing, and must not be presented to the user as one.
    """
    reported = [BasePath(path="/complete", kind=BasePathKind.CONTENT)]
    result = await verify_reported_paths(reported, _DenyingSFTP())
    assert result[0].state == BasePathState.UNVERIFIED


async def test_verify_reported_paths_with_no_sftp_reports_unverified_never_not_found():
    """No SSH connection to try at all (no host configured, undecryptable credentials, a
    connection attempt that itself failed) is the same "cannot look" fact as a permission
    error, never "the seedbox said no."
    """
    reported = [BasePath(path="/complete", kind=BasePathKind.WORKING)]
    result = await verify_reported_paths(reported, None)
    assert result[0].state == BasePathState.UNVERIFIED


async def test_verify_reported_paths_checks_each_path_independently():
    reported = [
        BasePath(path="/a", kind=BasePathKind.CONTENT),
        BasePath(path="/b", kind=BasePathKind.WORKING),
    ]
    result = await verify_reported_paths(reported, _FakeStatSFTP(existing={"/a"}))
    assert [r.state for r in result] == [BasePathState.VERIFIED, BasePathState.NOT_FOUND]


async def test_verify_reported_paths_never_verifies_a_tilde_path_directly():
    """No SFTP server expands `~`, so a `~`-reported path can never land as `verified` from the
    literal stat alone -- it always reports `not_found` first (finding #1's own root cause);
    what's new is the suggestion riding along with it, asserted by the tests below.
    """
    reported = [BasePath(path="~/downloads/rtorrent", kind=BasePathKind.WORKING)]
    sftp = _FakeTildeSFTP(
        existing={"/home/crzykidd/downloads/rtorrent"},
        realpath_map={"~/downloads/rtorrent": "/home/crzykidd/downloads/rtorrent"},
    )
    result = await verify_reported_paths(reported, sftp)
    assert result[0].state == BasePathState.NOT_FOUND


# --- `resolved_candidate` (spec §8.2 correction, finding #1, 2026-08-23) ----------------------


async def test_not_found_tilde_path_offers_its_ssh_home_expansion_when_it_exists():
    """The user's own proposed resolution: "It appears your ~ path pwd is xxx" -- offered, not
    applied. `remote_directory_error`'s literal stat still reports `not_found` for the raw `~`
    string; `resolved_candidate` carries the pre-filled suggestion the settings UI shows beside
    it.
    """
    reported = [BasePath(path="~/downloads/rtorrent", kind=BasePathKind.WORKING)]
    sftp = _FakeTildeSFTP(
        existing={"/home/crzykidd/downloads/rtorrent"},
        realpath_map={"~/downloads/rtorrent": "/home/crzykidd/downloads/rtorrent"},
    )
    result = await verify_reported_paths(reported, sftp)
    assert result[0].state == BasePathState.NOT_FOUND
    assert result[0].resolved_candidate == "/home/crzykidd/downloads/rtorrent"


async def test_not_found_tilde_path_offers_nothing_when_the_expansion_also_does_not_exist():
    """A `~` path whose expansion is itself absent must not manufacture a false suggestion --
    "the fix must not turn every miss into a false suggestion" (this task's own test list)."""
    reported = [BasePath(path="~/downloads/rtorrent", kind=BasePathKind.WORKING)]
    sftp = _FakeTildeSFTP(
        existing=set(),  # the expanded path does not exist either
        realpath_map={"~/downloads/rtorrent": "/home/crzykidd/downloads/rtorrent"},
    )
    result = await verify_reported_paths(reported, sftp)
    assert result[0].state == BasePathState.NOT_FOUND
    assert result[0].resolved_candidate is None


async def test_not_found_absolute_path_offers_no_candidate_at_all():
    """A genuinely missing *absolute* path (no `~`, no relative form) has nothing to expand --
    still reports `not_found`, `resolved_candidate` stays `None` rather than echoing the same
    already-absolute path back as its own "suggestion."
    """
    reported = [BasePath(path="/complete", kind=BasePathKind.CONTENT)]
    result = await verify_reported_paths(reported, _FakeStatSFTP(existing=set()))
    assert result[0].state == BasePathState.NOT_FOUND
    assert result[0].resolved_candidate is None


async def test_not_found_tilde_path_offers_no_candidate_when_realpath_itself_refuses():
    """An unusual server that refuses `realpath` outright -- `_resolve_tilde_candidate`'s own
    fallback (mirroring `core.browse.resolve_remote_dir`'s identical tolerance) is to offer
    nothing rather than raise and fail the whole detection pass.
    """
    reported = [BasePath(path="~/downloads/rtorrent", kind=BasePathKind.WORKING)]
    sftp = _FakeTildeSFTP(existing=set(), realpath_map={})  # realpath has no entry -- refuses
    result = await verify_reported_paths(reported, sftp)
    assert result[0].state == BasePathState.NOT_FOUND
    assert result[0].resolved_candidate is None


async def test_unverified_tilde_path_still_offers_a_resolved_candidate():
    """An ambiguous failure (permission, protocol) on the literal `~` stat must not lose the
    suggestion either -- `~` paths reach `unverified` too (module docstring's second branch),
    and the settings UI's "Accept anyway" for `unverified` must have something honest to prefill
    rather than falling back to the raw `~` string (finding #1's own stated constraint: "a `~`
    path must never be what gets stored").
    """

    class _DenyingButResolvingSFTP:
        async def stat(self, path: str) -> _FakeAttrs:
            raise asyncssh.SFTPPermissionDenied("nope")

        async def realpath(self, path: str) -> str:
            assert path == "~/downloads/rtorrent"
            return "/home/crzykidd/downloads/rtorrent"

    reported = [BasePath(path="~/downloads/rtorrent", kind=BasePathKind.WORKING)]
    result = await verify_reported_paths(reported, _DenyingButResolvingSFTP())
    assert result[0].state == BasePathState.UNVERIFIED
    assert result[0].resolved_candidate == "/home/crzykidd/downloads/rtorrent"


async def test_no_sftp_at_all_offers_no_candidate_for_a_tilde_path():
    """No SSH connection to try (`sftp is None`) can't resolve anything -- `unverified`, no
    candidate, exactly like every other `sftp is None` case in this module.
    """
    reported = [BasePath(path="~/downloads/rtorrent", kind=BasePathKind.WORKING)]
    result = await verify_reported_paths(reported, None)
    assert result[0].state == BasePathState.UNVERIFIED
    assert result[0].resolved_candidate is None
