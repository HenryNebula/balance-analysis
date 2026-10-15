"""The one repo-wide fixture: keep the default user overlay hermetic.

user_registry() falls back to ~/.local/share/statementledger/registry when
STATEMENTLEDGER_USER_REGISTRY is unset — a tree that exists on dev machines
with real distilled parsers. Without pinning, every env-unset keys() call
below would union the developer's own parsers (flaky at best; a real
shadow of a fixture key would change ground truth). Explicit
monkeypatch.setenv("STATEMENTLEDGER_USER_REGISTRY", ...) inside a test
still wins — env beats default.
"""

import pytest


@pytest.fixture(autouse=True)
def _hermetic_user_registry(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("STATEMENTLEDGER_USER_REGISTRY", raising=False)
