"""Command-line entry point for MClaude."""

import argparse
import json
import sys
from importlib.metadata import version
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from mclaude.agent import DEFAULT_MAX_ITERATIONS, run_agent
from mclaude.config import ConfigurationError, ModelConfig
from mclaude.permissions import PermissionGate, PermissionRequest
from mclaude.provider import ModelError


def _prompt_tool_permission(request: PermissionRequest, reason: str) -> bool:
    """Ask the terminal user to approve one tool call."""
    tool_input = json.dumps(request.tool_input, ensure_ascii=False, sort_keys=True)
    print(
        f"Permission required for tool '{request.tool_name}': {reason}",
        file=sys.stderr,
    )
    print(f"Input: {tool_input}", file=sys.stderr)
    print("Allow this tool call? [y/N] ", end="", file=sys.stderr, flush=True)
    answer = input()
    return answer.strip().casefold() in {"y", "yes"}


def _run_conversation(
    prompt: str | None,
    config: ModelConfig,
    *,
    interactive: bool,
    max_iterations: int,
) -> int:
    """Run one task or read successive turns using a shared message history."""
    history: list[dict[str, Any]] = []
    workspace = Path.cwd()
    permission_gate = PermissionGate(prompt=_prompt_tool_permission)
    exit_code = 0
    if interactive:
        print(
            "Interactive conversation. Enter /exit or /quit to leave; "
            "Ctrl+C cancels the current turn; at the input prompt it exits.",
            file=sys.stderr,
        )
    while True:
        if prompt is None:
            print("You> ", end="", file=sys.stderr, flush=True)
            try:
                prompt = input()
            except EOFError:
                print(file=sys.stderr)
                return exit_code
            if prompt.strip().casefold() in {"/exit", "/quit"}:
                return exit_code
            if not prompt.strip():
                prompt = None
                continue
        streamed = False

        def display_text(text: str) -> None:
            nonlocal streamed
            streamed = True
            print(text, end="", flush=True)

        try:
            response = run_agent(
                prompt,
                config,
                workspace=workspace,
                max_iterations=max_iterations,
                permission_gate=permission_gate,
                history=history,
                on_text=display_text,
            )
        except KeyboardInterrupt:
            if not interactive:
                raise
            print(
                "\nTurn cancelled. You can continue the conversation.", file=sys.stderr
            )
        except ModelError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            exit_code = 1
        else:
            if not streamed:
                print(response.text, flush=True)
            if response.truncated:
                print(
                    "Error: Output truncated; increase --max-tokens.", file=sys.stderr
                )
                exit_code = 1
        if not interactive:
            return exit_code
        prompt = None


def main(argv: list[str] | None = None) -> int:
    """Parse command-line options and run the application."""
    parser = argparse.ArgumentParser(
        prog="mclaude",
        description="MClaude: a Python coding agent built incrementally.",
        epilog="Set ANTHROPIC_API_KEY and ANTHROPIC_MODEL to run the agent.",
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {version('mclaude')}"
    )
    parser.add_argument("prompt", nargs="?", help="Question to send to the model")
    parser.add_argument(
        "-i",
        "--interactive",
        action="store_true",
        help="Start a conversation, optionally beginning with the supplied prompt",
    )
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
    parser.add_argument(
        "--max-iterations",
        type=int,
        default=DEFAULT_MAX_ITERATIONS,
        help=f"Maximum model requests per turn (default: {DEFAULT_MAX_ITERATIONS})",
    )
    args = parser.parse_args(argv)
    interactive = args.interactive or (args.prompt is None and sys.stdin.isatty())
    if args.prompt is None and not interactive:
        parser.print_help()
        return 0
    if args.prompt is not None and not args.prompt.strip():
        parser.error("The prompt must not be empty.")
    if args.max_iterations <= 0:
        parser.error("--max-iterations must be a positive integer.")
    load_dotenv(Path.cwd() / ".env", override=False)
    try:
        config = ModelConfig.from_env(
            model=args.model, max_tokens=args.max_tokens, timeout=args.timeout
        )
    except ConfigurationError as exc:
        parser.error(str(exc))
    try:
        return _run_conversation(
            args.prompt,
            config,
            interactive=interactive,
            max_iterations=args.max_iterations,
        )
    except KeyboardInterrupt:
        print("\nRequest interrupted.", file=sys.stderr)
        return 130
