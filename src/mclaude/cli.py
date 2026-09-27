"""Command-line entry point for MClaude."""

import argparse
import sys
from importlib.metadata import version

from mclaude.config import ConfigurationError, ModelConfig
from mclaude.provider import ModelError, complete


def main(argv: list[str] | None = None) -> int:
    """Parse command-line options and run the application."""
    parser = argparse.ArgumentParser(
        prog="mclaude",
        description="MClaude: a Python coding agent built incrementally.",
        epilog="Set ANTHROPIC_API_KEY and ANTHROPIC_MODEL to send a text request.",
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {version('mclaude')}"
    )
    parser.add_argument("prompt", nargs="?", help="Question to send to the model")
    parser.add_argument("--model", help="Model ID (overrides ANTHROPIC_MODEL)")
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=1024,
        help="Output token limit (default: 1024)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=60.0,
        help="Request timeout in seconds (default: 60)",
    )
    args = parser.parse_args(argv)
    if args.prompt is None:
        parser.print_help()
        return 0
    if not args.prompt.strip():
        parser.error("The prompt must not be empty.")
    try:
        config = ModelConfig.from_env(
            model=args.model, max_tokens=args.max_tokens, timeout=args.timeout
        )
    except ConfigurationError as exc:
        parser.error(str(exc))
    try:
        response = complete(args.prompt, config)
    except ModelError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Request interrupted.", file=sys.stderr)
        return 130
    print(response.text)
    if response.truncated:
        print("Error: Output truncated; increase --max-tokens.", file=sys.stderr)
        return 1
    return 0
