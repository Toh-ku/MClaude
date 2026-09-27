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
from mclaude.context import (
    DEFAULT_CONTEXT_BUDGET_TOKENS,
    ContextError,
    load_project_instructions,
)
from mclaude.permissions import PermissionGate, PermissionRequest
from mclaude.provider import ModelError
from mclaude.session import Session, SessionError, SessionStore


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
    context_budget_tokens: int,
    session: Session | None = None,
) -> int:
    """Run one task or read successive turns using a shared message history."""
    history: list[dict[str, Any]] = session.history if session is not None else []
    workspace = Path.cwd()
    permission_gate = PermissionGate(prompt=_prompt_tool_permission)
    exit_code = 0
    if interactive:
        print(
            "Interactive conversation. Enter /exit or /quit to leave; "
            "Ctrl+C cancels the current turn; at the input prompt it exits.",
            file=sys.stderr,
        )

    def record_history_event(event_type: str, payload: dict[str, Any]) -> None:
        if session is None:
            return
        if event_type == "message":
            session.record_message(payload)
        elif event_type == "tool_result":
            session.record_tool_result(payload)
        else:
            raise SessionError(f"Unsupported history event: {event_type}")

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
                context_budget_tokens=context_budget_tokens,
                permission_gate=permission_gate,
                history=history,
                on_text=display_text,
                on_history_event=record_history_event if session is not None else None,
            )
        except KeyboardInterrupt:
            if session is not None:
                session.record_turn("cancelled")
            if not interactive:
                raise
            print(
                "\nTurn cancelled. You can continue the conversation.", file=sys.stderr
            )
        except ModelError as exc:
            if session is not None:
                session.record_turn("error")
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
            if session is not None:
                session.record_turn("truncated" if response.truncated else "ok")
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
    resume_group = parser.add_mutually_exclusive_group()
    resume_group.add_argument(
        "--continue",
        dest="continue_session",
        action="store_true",
        help="Resume the most recent session for the current workspace",
    )
    resume_group.add_argument(
        "--resume",
        metavar="SESSION_ID",
        help="Resume a specific session for the current workspace",
    )
    parser.add_argument(
        "--no-persist",
        action="store_true",
        help="Run an interactive conversation without saving it",
    )
    parser.add_argument(
        "--show-instructions",
        action="store_true",
        help="Show project instruction sources for the current workspace and exit",
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
    parser.add_argument(
        "--context-budget",
        type=int,
        default=DEFAULT_CONTEXT_BUDGET_TOKENS,
        metavar="TOKENS",
        help=(
            "Estimated input/output context budget "
            f"(default: {DEFAULT_CONTEXT_BUDGET_TOKENS})"
        ),
    )
    args = parser.parse_args(argv)
    if args.show_instructions:
        try:
            instructions = load_project_instructions(Path.cwd())
        except ContextError as exc:
            parser.error(str(exc))
        if not instructions.sources:
            print("No project instruction files apply to this workspace.")
        else:
            for source in instructions.sources:
                relative = source.path.relative_to(instructions.workspace)
                scope = source.scope.relative_to(instructions.workspace)
                print(f"{relative} (scope: {scope or Path('.')})")
        return 0
    if args.no_persist and (args.continue_session or args.resume):
        parser.error("--no-persist cannot be combined with --continue or --resume.")
    interactive = (
        args.interactive
        or args.continue_session
        or args.resume is not None
        or (args.prompt is None and sys.stdin.isatty())
    )
    if args.prompt is None and not interactive:
        parser.print_help()
        return 0
    if args.prompt is not None and not args.prompt.strip():
        parser.error("The prompt must not be empty.")
    if args.max_iterations <= 0:
        parser.error("--max-iterations must be a positive integer.")
    if args.context_budget <= args.max_tokens:
        parser.error("--context-budget must be greater than --max-tokens.")
    load_dotenv(Path.cwd() / ".env", override=False)
    try:
        config = ModelConfig.from_env(
            model=args.model, max_tokens=args.max_tokens, timeout=args.timeout
        )
    except ConfigurationError as exc:
        parser.error(str(exc))
    session: Session | None = None
    try:
        if interactive and not args.no_persist:
            store = SessionStore()
            workspace = Path.cwd()
            if args.resume is not None:
                session = store.resume(args.resume, workspace)
                print(f"Resumed session: {session.id}", file=sys.stderr)
            elif args.continue_session:
                session = store.continue_latest(workspace)
                print(f"Resumed session: {session.id}", file=sys.stderr)
            else:
                session = store.create(workspace, config.model)
                print(f"Session: {session.id}", file=sys.stderr)
            if session.recovery_warning:
                print(
                    f"Session recovery warning: {session.recovery_warning}",
                    file=sys.stderr,
                )
        try:
            return _run_conversation(
                args.prompt,
                config,
                interactive=interactive,
                max_iterations=args.max_iterations,
                context_budget_tokens=args.context_budget,
                session=session,
            )
        except KeyboardInterrupt:
            print("\nRequest interrupted.", file=sys.stderr)
            return 130
    except SessionError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    finally:
        if session is not None:
            session.close()
