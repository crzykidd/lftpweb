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
