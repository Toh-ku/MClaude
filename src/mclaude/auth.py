"""User-level API credentials and terminal login configuration."""

import getpass
import json
import os
import sys
import tempfile
import warnings
from pathlib import Path

from mclaude.config import ConfigurationError, ModelConfig


def config_path() -> Path:
    override = os.environ.get("MCLAUDE_CONFIG_DIR")
    if override:
        return Path(override).expanduser().resolve() / "config.json"
    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA")
        base = Path(local) if local else Path.home() / "AppData" / "Local"
        return base / "MClaude" / "config.json"
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return base / "mclaude" / "config.json"


class ConfigStore:
    """Store only login settings; replace atomically after validation."""

    def __init__(self, path: Path | None = None):
        self.path = path if path is not None else config_path()

    def load(self) -> dict[str, str]:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        except (OSError, UnicodeError) as exc:
            raise ConfigurationError("Cannot read saved login configuration.") from exc
        try:
            value = json.loads(raw)
            if (
                not isinstance(value, dict)
                or set(value) != {"version", "api_key", "model", "base_url"}
                or type(value["version"]) is not int
                or value["version"] != 1
                or any(
                    not isinstance(value[key], str)
                    for key in ("api_key", "model", "base_url")
                )
            ):
                raise ValueError
            settings = {
                key: value[key].strip() for key in ("api_key", "model", "base_url")
            }
            ModelConfig(
                api_key=settings["api_key"],
                model=settings["model"],
                base_url=settings["base_url"] or None,
            )
            return settings
        except (ValueError, TypeError) as exc:
            raise ConfigurationError(
                "Saved login configuration is invalid; run 'mclaude login' "
                "to replace it or 'mclaude logout' to remove it."
            ) from exc

    def save(self, settings: dict[str, str]) -> None:
        config = ModelConfig(
            api_key=settings["api_key"].strip(),
            model=settings["model"].strip(),
            base_url=settings.get("base_url", "").strip() or None,
        )
        value = {
            "version": 1,
            "api_key": config.api_key,
            "model": config.model,
            "base_url": config.base_url or "",
        }
        temporary: Path | None = None
        try:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=".login-",
                suffix=".tmp",
                delete=False,
            ) as file:
                temporary = Path(file.name)
                if os.name != "nt":
                    os.chmod(temporary, 0o600)
                json.dump(value, file, ensure_ascii=False, indent=2)
                file.write("\n")
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, self.path)
        except OSError as exc:
            raise ConfigurationError("Cannot save login configuration.") from exc
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass  # Preserve the original save error without exposing details.

    def logout(self) -> bool:
        try:
            self.path.unlink()
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise ConfigurationError(
                "Cannot remove saved login configuration."
            ) from exc
        return True


def _read_setting(label: str, default: str = "") -> str:
    print(
        f"{label}" + (f" [{default}]" if default else "") + ": ",
        end="",
        file=sys.stderr,
        flush=True,
    )
    return input().strip() or default


def login(
    store: ConfigStore,
    *,
    model: str | None = None,
    base_url: str | None = None,
) -> dict[str, str]:
    if not sys.stdin.isatty():
        raise ConfigurationError(
            "Run 'mclaude login' in an interactive terminal, or set "
            "ANTHROPIC_API_KEY and ANTHROPIC_MODEL for non-interactive use."
        )
    print(
        "Configure API login. The API key is stored locally in "
        f"{store.path}. Ctrl+C cancels.\n"
        "Saving configuration does not verify credentials with the API.",
        file=sys.stderr,
    )
    try:
        # Fail instead of falling back to visible password input.
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            api_key = getpass.getpass("API key (hidden): ", stream=sys.stderr).strip()
        settings = {
            "api_key": api_key,
            "model": _read_setting(
                "Model ID", model or os.environ.get("ANTHROPIC_MODEL", "").strip()
            ),
            "base_url": _read_setting(
                "API base URL (blank = official API)", base_url or ""
            ),
        }
    except getpass.GetPassWarning as exc:
        raise ConfigurationError(
            "Cannot securely read the API key in this terminal."
        ) from exc
    except EOFError as exc:
        raise ConfigurationError("Login cancelled: terminal input ended.") from exc
    store.save(settings)
    print(f"Login configuration saved to {store.path}.", file=sys.stderr)
    return settings
