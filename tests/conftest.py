from __future__ import annotations

import pytest

from fake_arr import fake_arr_server  # noqa: F401 - re-exported for auto-discovery below

from lftpweb.config import settings

# `fake_arr_server` (tests/fake_arr.py) is re-exported here, not imported directly by each test
# module that uses it, so it is auto-discovered for every test file the same way `isolated_config`
# below already is -- a test module importing a `@pytest.fixture`-decorated function under the
# same name it uses as a request parameter makes ruff's pyflakes read the parameter as
# "redefining an unused import" (F811); routing the fixture through conftest.py is pytest's own
# idiom for sharing a fixture defined outside conftest.py, and avoids the false positive
# entirely by construction -- no test module needs to import the name at all.


@pytest.fixture
def isolated_config(tmp_path, monkeypatch):
    """Point LFTPWEB_CONFIG_DIR at a throwaway directory for the duration of a test."""
    monkeypatch.setattr(settings, "config_dir", str(tmp_path))
    return tmp_path
