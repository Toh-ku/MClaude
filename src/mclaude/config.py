"""Configuration for a single model request."""

import math
import os
from dataclasses import dataclass, field


class ConfigurationError(ValueError):
    """Required configuration is absent or invalid."""


@dataclass(frozen=True)
class ModelConfig:
    api_key: str = field(repr=False)
    model: str
    max_tokens: int = 1024
    timeout: float = 60.0

    def __post_init__(self) -> None:
        if not self.api_key.strip():
            raise ConfigurationError("Set ANTHROPIC_API_KEY before sending a request.")
        if not self.model.strip():
            raise ConfigurationError("Set ANTHROPIC_MODEL or pass --model.")
        if self.max_tokens <= 0:
            raise ConfigurationError("--max-tokens must be a positive integer.")
        if not math.isfinite(self.timeout) or self.timeout <= 0:
            raise ConfigurationError("--timeout must be a finite positive number.")

    @classmethod
    def from_env(
        cls,
        *,
        model: str | None = None,
        max_tokens: int = 1024,
        timeout: float = 60.0,
    ) -> "ModelConfig":
        return cls(
            api_key=os.environ.get("ANTHROPIC_API_KEY", "").strip(),
            model=(
                model if model is not None else os.environ.get("ANTHROPIC_MODEL", "")
            ).strip(),
            max_tokens=max_tokens,
            timeout=timeout,
        )
