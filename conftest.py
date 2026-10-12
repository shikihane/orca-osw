import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "skill"))


@pytest.fixture(autouse=True)
def _hermetic_jev_env(monkeypatch):
    """Tests never reach the network: the operator's Jev credentials and
    proxy variables are removed for every test (the default mode is
    enforce, so a leaked key would turn gate tests into live calls)."""
    for name in (
        "TYPESAFE_API_KEY", "TYPESAFE_BASE_URL", "OSW_JEV_MODE",
        "HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy",
        "PI_PROXY", "DOCKER_SANDBOXES_PROXY",
    ):
        monkeypatch.delenv(name, raising=False)
