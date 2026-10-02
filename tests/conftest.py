"""Keep tests independent of local credentials and external services."""

import pytest


@pytest.fixture(autouse=True)
def clear_model_environment(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    for name in (
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_MODEL",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_LOG",
        "MCLAUDE_STATE_DIR",
        "MCLAUDE_CONFIG_DIR",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MCLAUDE_STATE_DIR", str(tmp_path / ".mclaude-state"))
    monkeypatch.setenv("MCLAUDE_CONFIG_DIR", str(tmp_path / ".mclaude-config"))
