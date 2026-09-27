"""Anthropic Messages API access and response normalization."""

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from anthropic import (
    Anthropic,
    APIConnectionError,
    APIError,
    APIStatusError,
    APITimeoutError,
    AsyncAnthropic,
)

from mclaude.config import ModelConfig

TextCallback = Callable[[str], None]


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


async def _consume_stream(stream: Any, on_text: TextCallback) -> Any:
    """Display text while the SDK assembles a complete, validated message."""
    finished = False
    open_blocks: set[int] = set()
    tool_json: dict[int, str] = {}
    seen_text = False
    try:
        async for event in stream:
            if event.type == "content_block_start":
                open_blocks.add(event.index)
                if event.content_block.type == "text":
                    if seen_text:
                        on_text("\n")
                    seen_text = True
                    if event.content_block.text:
                        on_text(event.content_block.text)
            elif event.type == "content_block_delta":
                if event.delta.type == "text_delta":
                    on_text(event.delta.text)
                elif event.delta.type == "input_json_delta":
                    tool_json[event.index] = (
                        tool_json.get(event.index, "") + event.delta.partial_json
                    )
            elif event.type == "content_block_stop":
                open_blocks.remove(event.index)
            elif event.type == "message_stop":
                finished = True
        if not finished or open_blocks:
            raise ModelError(
                "The response stream ended before the message was complete."
            )
        message = await stream.get_final_message()
        if message.stop_reason != "max_tokens":
            # SDK partial JSON parsing also accepts unfinished objects. Require
            # complete JSON before any of these tool calls can be executed.
            for value in tool_json.values():
                if not isinstance(json.loads(value), dict):
                    raise ModelError("The model returned invalid tool arguments.")
        return message
    except (ModelError, APIError):
        raise
    except Exception as exc:
        # Stream transport/decoder errors can include raw response data.
        raise ModelError(
            "The response stream failed or contained invalid data."
        ) from exc


async def _stream_message(
    options: dict[str, Any], config: ModelConfig, on_text: TextCallback
) -> Any:
    # asyncio.Runner cancels the main task on Ctrl+C and wakes the event loop,
    # including while Windows is waiting for response headers or socket data.
    async with AsyncAnthropic(
        api_key=config.api_key,
        timeout=config.timeout,
        max_retries=0,
    ) as client:
        async with client.messages.stream(**options) as stream:
            return await _consume_stream(stream, on_text)


def create_message(
    messages: list[dict[str, Any]],
    config: ModelConfig,
    *,
    tools: list[dict[str, Any]] | None = None,
    on_text: TextCallback | None = None,
) -> ModelResponse:
    """Send a message request and normalize the content used by the agent."""
    try:
        options = {
            "model": config.model,
            "max_tokens": config.max_tokens,
            "messages": messages,
            **({"tools": tools} if tools else {}),
        }
        if on_text is None:
            with Anthropic(
                api_key=config.api_key,
                timeout=config.timeout,
                max_retries=0,
            ) as client:
                message = client.messages.create(**options)
        else:
            message = asyncio.run(_stream_message(options, config, on_text))
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
