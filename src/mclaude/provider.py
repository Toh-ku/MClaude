"""Anthropic Messages API access and response normalization."""

from dataclasses import dataclass
from typing import Any

from anthropic import (
    Anthropic,
    APIConnectionError,
    APIError,
    APIStatusError,
    APITimeoutError,
)

from mclaude.config import ModelConfig


class ModelError(RuntimeError):
    """A model request failed or did not produce a usable text response."""


@dataclass(frozen=True)
class TextResponse:
    text: str
    truncated: bool


@dataclass(frozen=True)
class TextBlock:
    text: str


@dataclass(frozen=True)
class ToolUseBlock:
    id: str
    name: str
    input: object


ContentBlock = TextBlock | ToolUseBlock


@dataclass(frozen=True)
class ModelResponse:
    content: tuple[ContentBlock, ...]
    stop_reason: str | None


def create_message(
    messages: list[dict[str, Any]],
    config: ModelConfig,
    *,
    tools: list[dict[str, Any]] | None = None,
) -> ModelResponse:
    """Send a message request and normalize the content used by the agent."""
    try:
        with Anthropic(
            api_key=config.api_key,
            timeout=config.timeout,
            max_retries=0,
        ) as client:
            message = client.messages.create(
                model=config.model,
                max_tokens=config.max_tokens,
                messages=messages,
                **({"tools": tools} if tools else {}),
            )
    except APITimeoutError as exc:
        raise ModelError("Request timed out; try again or increase --timeout.") from exc
    except APIConnectionError as exc:
        raise ModelError(
            "Cannot connect to the API; check your network and ANTHROPIC_BASE_URL."
        ) from exc
    except APIStatusError as exc:
        explanations = {
            400: "Invalid request; check the model and request parameters.",
            401: "Authentication failed; check ANTHROPIC_API_KEY.",
            403: "Access denied; check your API key permissions and model access.",
            404: "Model or endpoint not found; check the model and ANTHROPIC_BASE_URL.",
            429: "API rate limit or quota reached; try later or check your quota.",
        }
        detail = explanations.get(
            exc.status_code,
            "API service unavailable; try again later."
            if exc.status_code >= 500
            else "API rejected the request; check your configuration.",
        )
        # Do not include server bodies, which may echo credentials or prompt text.
        raise ModelError(f"HTTP {exc.status_code}: {detail}") from exc
    except APIError as exc:
        raise ModelError("The API returned an invalid response.") from exc

    content: list[ContentBlock] = []
    for block in message.content:
        if block.type == "text":
            content.append(TextBlock(text=block.text))
        elif block.type == "tool_use":
            content.append(
                ToolUseBlock(id=block.id, name=block.name, input=block.input)
            )
    return ModelResponse(content=tuple(content), stop_reason=message.stop_reason)


def complete(prompt: str, config: ModelConfig) -> TextResponse:
    """Send one text-only request, retained as a small public convenience API."""
    if not prompt.strip():
        raise ModelError("The prompt must not be empty.")
    message = create_message([{"role": "user", "content": prompt}], config)

    if message.stop_reason not in {"end_turn", "max_tokens"}:
        raise ModelError("The model did not finish a text response.")
    text = "\n".join(
        block.text for block in message.content if isinstance(block, TextBlock)
    )
    if not text.strip():
        raise ModelError("The model returned no text.")
    return TextResponse(text=text, truncated=message.stop_reason == "max_tokens")
