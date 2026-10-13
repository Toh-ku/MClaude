"""Configuration and validation before any network request."""

import pytest

from mclaude import cli
from mclaude.config import ConfigurationError, ModelConfig


def test_model_and_credentials_from_environment(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", " test-secret ")
    monkeypatch.setenv("ANTHROPIC_MODEL", " env-model ")
    config = ModelConfig.from_env()
    assert config.api_key == "test-secret"
    assert config.model == "env-model"
    assert "test-secret" not in repr(config)
    assert ModelConfig.from_env(model="override").model == "override"


@pytest.mark.parametrize(
    ("key", "model", "expected"),
    [("", "test", "ANTHROPIC_API_KEY"), ("test-secret", "  ", "ANTHROPIC_MODEL")],
)
def test_missing_configuration(key, model, expected, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", key)
    monkeypatch.setenv("ANTHROPIC_MODEL", model)
    with pytest.raises(ConfigurationError, match=expected):
        ModelConfig.from_env()


@pytest.mark.parametrize(
    "args",
    [
        ["   "],
        ["Hello", "--model", " "],
        ["Hello", "--max-tokens", "0"],
        ["Hello", "--max-tokens", "-1"],
        ["Hello", "--timeout", "0"],
        ["Hello", "--timeout", "-1"],
        ["Hello", "--timeout", "nan"],
        ["Hello", "--timeout", "inf"],
        ["Hello", "--request-retries", "-1"],
        ["Hello", "--request-retries", "11"],
        ["Hello", "--retry-delay", "-1"],
        ["Hello", "--retry-delay", "nan"],
    ],
)
def test_invalid_cli_values_never_call_model(args, monkeypatch, capsys):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-secret")
    monkeypatch.setenv("ANTHROPIC_MODEL", "test-model")

    def unexpected_call(*args, **kwargs):
        pytest.fail("Invalid input must not reach the model")

    monkeypatch.setattr(cli, "run_agent", unexpected_call)
    with pytest.raises(SystemExit) as error:
        cli.main(args)
    assert error.value.code == 2
    assert "error:" in capsys.readouterr().err


def test_cli_missing_key_is_actionable(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_MODEL", raising=False)
    with pytest.raises(SystemExit) as error:
        cli.main(["Hello"])
    assert error.value.code == 2
    assert "ANTHROPIC_API_KEY" in capsys.readouterr().err


def test_cli_rejects_removed_env_file_option(tmp_path, monkeypatch, capsys):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "ANTHROPIC_API_KEY=dotenv-secret\nANTHROPIC_MODEL=dotenv-model\n",
        encoding="utf-8",
    )
    with pytest.raises(SystemExit) as error:
        cli.main(["Hello", "--env-file", str(env_file)])
    assert error.value.code == 2
    assert "unrecognized arguments: --env-file" in capsys.readouterr().err
