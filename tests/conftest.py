from __future__ import annotations

import pytest

from lftpweb.config import settings


@pytest.fixture
def isolated_config(tmp_path, monkeypatch):
    """Point LFTPWEB_CONFIG_DIR at a throwaway directory for the duration of a test."""
    monkeypatch.setattr(settings, "config_dir", str(tmp_path))
    return tmp_path
