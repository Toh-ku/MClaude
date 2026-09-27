"""Single-turn text requests through the Anthropic Messages API."""

from dataclasses import dataclass

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


def complete(prompt: str, config: ModelConfig) -> TextResponse:
    """Send one request, with no tools, streaming, or automatic retries."""
    if not prompt.strip():
        raise ModelError("The prompt must not be empty.")
    try:
        with Anthropic(
            api_key=config.api_key,
            timeout=config.timeout,
            max_retries=0,
        ) as client:
            message = client.messages.create(
                model=config.model,
                max_tokens=config.max_tokens,
                messages=[{"role": "user", "content": prompt}],
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

    if message.stop_reason not in {"end_turn", "max_tokens"}:
        raise ModelError("The model did not finish a text response.")
    text = "\n".join(block.text for block in message.content if block.type == "text")
    if not text.strip():
        raise ModelError("The model returned no text.")
    return TextResponse(text=text, truncated=message.stop_reason == "max_tokens")
