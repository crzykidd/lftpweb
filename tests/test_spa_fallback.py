from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from lftpweb.config import settings
from lftpweb.main import create_app


@pytest.fixture
def spa_static_dir(tmp_path, monkeypatch, isolated_config):
    """A fake built-SPA directory, wired in via settings.static_dir. main.create_app()
    checks static_dir at app-creation time, so the app under test must be built after
    this fixture patches the setting.
    """
    static_dir = tmp_path / "static"
    (static_dir / "assets").mkdir(parents=True)
    (static_dir / "index.html").write_text("<html><body>lftpweb shell</body></html>")
    (static_dir / "assets" / "app.js").write_text("console.log('app')")
    monkeypatch.setattr(settings, "static_dir", str(static_dir))
    return static_dir


def test_deep_link_route_serves_the_spa_shell_not_a_404(spa_static_dir):
    app = create_app()
    with TestClient(app) as client:
        resp = client.get("/settings/logs")
        assert resp.status_code == 200
        assert "lftpweb shell" in resp.text


def test_built_asset_is_served_from_assets_mount(spa_static_dir):
    app = create_app()
    with TestClient(app) as client:
        resp = client.get("/assets/app.js")
        assert resp.status_code == 200
        assert "console.log" in resp.text


def test_unknown_api_route_is_a_404_not_the_spa_shell(spa_static_dir):
    app = create_app()
    with TestClient(app) as client:
        resp = client.get("/api/does-not-exist")
        assert resp.status_code == 404


def test_encoded_traversal_cannot_read_a_file_outside_the_static_dir(spa_static_dir, tmp_path):
    """Finding S1: the SPA catch-all must not serve a path that escapes static_dir. The
    percent-encoded `..%2f` form is the exploitable one -- it reaches the handler decoded to
    `../` but is *not* `..`-normalized away first (a literal `/../` is collapsed by the client
    before the request is even sent), so this asserts the encoded form specifically.
    """
    secret = tmp_path / "outside" / "secret.txt"
    secret.parent.mkdir()
    secret.write_text("TOP SECRET -- must never be served")
    # static_dir is tmp_path/"static"; one level up then into "outside".
    payload = "..%2foutside%2fsecret.txt"

    app = create_app()
    with TestClient(app) as client:
        resp = client.get(f"/{payload}")
        # The escape attempt falls through to the SPA shell, never the secret.
        assert resp.status_code == 200
        assert "TOP SECRET" not in resp.text
        assert "lftpweb shell" in resp.text


def test_encoded_traversal_to_an_absolute_system_path_is_refused(spa_static_dir):
    """A second, install-independent traversal payload aimed above the static root entirely."""
    app = create_app()
    with TestClient(app) as client:
        resp = client.get("/..%2f..%2f..%2f..%2f..%2fetc/hostname")
        assert resp.status_code == 200
        assert "lftpweb shell" in resp.text
