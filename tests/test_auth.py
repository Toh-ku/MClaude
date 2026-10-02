"""Login persistence, first-use setup, logout and credential isolation."""

import json
import os
import sys

import pytest

from mclaude import auth, cli
from mclaude.agent import AgentResponse
from mclaude.auth import ConfigStore
from mclaude.config import ConfigurationError, ModelConfig


@pytest.fixture
def store():
    return ConfigStore()


@pytest.fixture
def terminal(monkeypatch):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(auth.getpass, "getpass", lambda *a, **k: "new-secret")
    entries = iter(["new-model", "https://example.test"])
    monkeypatch.setattr("builtins.input", lambda: next(entries))


def settings(key="saved-secret", model="saved-model", url="https://saved.test"):
    return {"api_key": key, "model": model, "base_url": url}


def test_store_roundtrip_and_logout(store):
    assert store.load() == {}
    store.save(settings())
    assert store.load() == settings()
    store.save(settings(key="replacement"))
    assert store.load()["api_key"] == "replacement"
    assert not list(store.path.parent.glob(".login-*.tmp"))
    assert store.logout() is True
    assert store.load() == {}
    assert store.logout() is False


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
def test_credentials_are_owner_only(store):
    store.save(settings())
    assert store.path.stat().st_mode & 0o777 == 0o600
    assert store.path.parent.stat().st_mode & 0o777 == 0o700


def test_config_path_is_user_level(monkeypatch, tmp_path):
    monkeypatch.delenv("MCLAUDE_CONFIG_DIR")
    if os.name == "nt":
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
        expected = tmp_path / "MClaude" / "config.json"
    else:
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        expected = tmp_path / "mclaude" / "config.json"
    assert auth.config_path() == expected


@pytest.mark.parametrize(
    "raw",
    ["not json: leaked-secret", "[]", '{"version": 99}', '{"api_key": 123}'],
)
def test_invalid_saved_configuration_is_actionable_and_redacted(store, raw):
    store.path.parent.mkdir(parents=True)
    store.path.write_text(raw, encoding="utf-8")
    with pytest.raises(ConfigurationError, match="mclaude login") as error:
        store.load()
    assert "leaked-secret" not in str(error.value)
    assert store.logout()


def test_failed_replace_preserves_existing_login(store, monkeypatch):
    store.save(settings())

    def fail(*args):
        raise PermissionError("secret filesystem detail")

    monkeypatch.setattr(auth.os, "replace", fail)
    with pytest.raises(ConfigurationError, match="Cannot save") as error:
        store.save(settings(key="replacement"))
    assert "secret filesystem detail" not in str(error.value)
    assert store.load() == settings()
    assert not list(store.path.parent.glob(".login-*.tmp"))


def test_login_saves_hidden_key_without_network(terminal, store, monkeypatch, capsys):
    monkeypatch.setattr(
        cli, "run_agent", lambda *a, **k: pytest.fail("Login must not run the agent")
    )
    assert cli.main(["login"]) == 0
    assert store.load() == settings("new-secret", "new-model", "https://example.test")
    output = capsys.readouterr()
    assert "new-secret" not in output.out + output.err
    assert "saved" in output.err


def test_login_defaults_and_blank_url(terminal, store, monkeypatch):
    entries = iter(["", ""])
    monkeypatch.setattr("builtins.input", lambda: next(entries))
    assert cli.main(["login", "--model", "chosen-model"]) == 0
    assert store.load() == settings("new-secret", "chosen-model", "")


def test_login_repairs_corrupt_configuration(terminal, store):
    store.path.parent.mkdir(parents=True)
    store.path.write_text("corrupt", encoding="utf-8")
    assert cli.main(["login"]) == 0
    assert store.load()["api_key"] == "new-secret"


@pytest.mark.parametrize("failure", [KeyboardInterrupt, EOFError])
def test_cancelled_login_preserves_credentials(terminal, store, monkeypatch, failure):
    store.save(settings())

    def cancel(*args, **kwargs):
        raise failure

    monkeypatch.setattr(auth.getpass, "getpass", cancel)
    assert cli.main(["login"]) == (130 if failure is KeyboardInterrupt else 1)
    assert store.load() == settings()


def test_getpass_does_not_fall_back_to_echo(terminal, store, monkeypatch):
    import warnings

    def unsafe(*args, **kwargs):
        warnings.warn("echo unavailable", auth.getpass.GetPassWarning, stacklevel=2)
        pytest.fail("Do not continue with visible password input")

    monkeypatch.setattr(auth.getpass, "getpass", unsafe)
    assert cli.main(["login"]) == 1
    assert not store.path.exists()


@pytest.mark.parametrize("command", [["login"], ["Hello"]])
def test_noninteractive_missing_credentials_does_not_prompt(
    command, store, monkeypatch, capsys
):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(
        auth.getpass, "getpass", lambda *a, **k: pytest.fail("Must not prompt")
    )
    if command == ["login"]:
        assert cli.main(command) == 1
    else:
        with pytest.raises(SystemExit) as error:
            cli.main(command)
        assert error.value.code == 2
    assert "mclaude login" in capsys.readouterr().err
    assert not store.path.exists()


def test_first_use_configures_then_runs_same_task(
    terminal, store, monkeypatch, tmp_path, capsys
):
    monkeypatch.chdir(tmp_path)
    # An old project dotenv file must not silently supply credentials.
    (tmp_path / ".env").write_text(
        "ANTHROPIC_API_KEY=old-secret\nANTHROPIC_MODEL=old-model\n", encoding="utf-8"
    )
    received = []

    def agent(prompt, config, **kwargs):
        received.append((prompt, config))
        return AgentResponse("done")

    monkeypatch.setattr(cli, "run_agent", agent)
    assert cli.main(["My task"]) == 0
    assert received[0][0] == "My task"
    assert received[0][1].api_key == "new-secret"
    assert received[0][1].base_url == "https://example.test"
    assert store.load()["model"] == "new-model"
    assert capsys.readouterr().out == "done\n"


def test_saved_login_is_used_across_workspaces(store, monkeypatch, tmp_path):
    store.save(settings())
    received = []

    def agent(prompt, config, **kwargs):
        received.append(config)
        return AgentResponse("done")

    monkeypatch.setattr(cli, "run_agent", agent)
    for name in ("one", "two"):
        directory = tmp_path / name
        directory.mkdir()
        monkeypatch.chdir(directory)
        assert cli.main(["Task"]) == 0
    assert [config.api_key for config in received] == ["saved-secret"] * 2


def test_environment_and_model_option_override_saved_config(store, monkeypatch):
    store.save(settings())
    monkeypatch.setenv("ANTHROPIC_API_KEY", "env-secret")
    monkeypatch.setenv("ANTHROPIC_MODEL", "env-model")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://env.test")
    config = ModelConfig.from_env(model="cli-model", saved=store.load())
    assert config.api_key == "env-secret"
    assert config.model == "cli-model"
    assert config.base_url == "https://env.test"
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "")
    assert ModelConfig.from_env(saved=store.load()).base_url is None


def test_logout_keeps_sessions_and_explains_environment(store, monkeypatch, tmp_path):
    store.save(settings())
    session = tmp_path / "session.jsonl"
    session.write_text("existing history", encoding="utf-8")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "env-secret")
    assert cli.main(["logout"]) == 0
    assert not store.path.exists()
    assert session.read_text() == "existing history"
    assert os.environ["ANTHROPIC_API_KEY"] == "env-secret"
    assert cli.main(["logout"]) == 0


@pytest.mark.parametrize("command", [["login", "--help"], ["logout", "--help"]])
def test_auth_help_does_not_create_configuration(command, store, capsys):
    with pytest.raises(SystemExit) as error:
        cli.main(command)
    assert error.value.code == 0
    assert "usage:" in capsys.readouterr().out
    assert not store.path.exists()


@pytest.mark.parametrize(
    "url", ["ftp://example.test", "https://user:secret@example.test", "https://", "bad"]
)
def test_invalid_endpoint_never_overwrites_login(store, url):
    store.save(settings())
    with pytest.raises(ConfigurationError, match="HTTP"):
        store.save(settings(url=url))
    assert store.load() == settings()


def test_missing_env_file_fails_without_login(store, monkeypatch):
    monkeypatch.setattr(
        auth.getpass, "getpass", lambda *a, **k: pytest.fail("Must not prompt")
    )
    with pytest.raises(SystemExit) as error:
        cli.main(["Task", "--env-file", str(store.path.parent / "missing.env")])
    assert error.value.code == 2
    assert not store.path.exists()


def test_saved_json_has_only_login_settings(store):
    store.save(settings())
    assert set(json.loads(store.path.read_text(encoding="utf-8"))) == {
        "version",
        "api_key",
        "model",
        "base_url",
    }
