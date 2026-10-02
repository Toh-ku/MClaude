"""Command-line entry point for MClaude."""

import argparse
import json
import os
import sys
import time
from importlib.metadata import version
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from mclaude.agent import DEFAULT_MAX_ITERATIONS, run_agent
from mclaude.auth import ConfigStore, login
from mclaude.config import ConfigurationError, ModelConfig
from mclaude.context import (
    DEFAULT_CONTEXT_BUDGET_TOKENS,
    ContextError,
    load_project_instructions,
)
from mclaude.hooks import HookError, HookRunner
from mclaude.mcp import MCPError, MCPRegistry
from mclaude.permissions import PermissionGate, PermissionRequest
from mclaude.provider import ModelError
from mclaude.session import Session, SessionError, SessionStore
from mclaude.skills import SkillCatalog
from mclaude.tasks import TaskBoard
from mclaude.terminal import TerminalStatus, supports_status_view


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
    planning: bool = False,
    hooks: HookRunner | None = None,
    mcp: MCPRegistry | None = None,
    subagent_budget: int = 8,
    read_workers: int = 4,
    terminal_status: TerminalStatus | None = None,
) -> int:
    """Run one task or read successive turns using a shared message history."""
    history: list[dict[str, Any]] = session.history if session is not None else []
    if terminal_status is not None:
        terminal_status.history = history
    task_board = session.task_board if session is not None else TaskBoard()
    workspace = Path.cwd()
    permission_gate = PermissionGate(prompt=_prompt_tool_permission)
    exit_code = 0
    turn_number = 0
    if interactive:
        print(
            "Interactive conversation. Enter /exit or /quit to leave; "
            "Ctrl+C cancels the current turn; at the input prompt it exits.",
            file=sys.stderr,
        )
        if terminal_status is not None:
            terminal_status.ready()

    def record_history_event(event_type: str, payload: dict[str, Any]) -> None:
        if event_type == "tasks" and terminal_status is not None:
            terminal_status.update_tasks(payload["tasks"])
        if session is None:
            return
        if event_type == "message":
            session.record_message(payload)
        elif event_type == "tool_result":
            session.record_tool_result(payload)
        elif event_type == "compaction":
            session.record_compaction(payload["history"])
        elif event_type == "tasks":
            session.record_tasks(payload["tasks"])
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
            if prompt.strip().casefold() == "/tasks":
                print(task_board.render())
                if terminal_status is not None:
                    terminal_status.event("tasks", "displayed current task board")
                prompt = None
                continue
            if prompt.strip().casefold() in {"/plan", "/execute"}:
                planning = prompt.strip().casefold() == "/plan"
                if planning and mcp is not None:
                    mcp.close()
                print(
                    f"Mode: {'planning' if planning else 'execution'}", file=sys.stderr
                )
                if terminal_status is not None:
                    terminal_status.set_mode(planning)
                prompt = None
                continue
        streamed = False
        turn_number += 1
        turn_started = time.monotonic()
        if terminal_status is not None:
            terminal_status.event("turn", f"{turn_number} started")

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
                task_board=task_board,
                planning=planning,
                hooks=hooks,
                mcp=mcp,
                subagent_budget=subagent_budget,
                read_workers=read_workers,
                on_text=display_text,
                on_history_event=(
                    record_history_event
                    if session is not None or terminal_status is not None
                    else None
                ),
                on_status_event=(
                    terminal_status.agent_event if terminal_status is not None else None
                ),
            )
        except KeyboardInterrupt:
            if session is not None:
                session.record_turn("cancelled")
            if not interactive:
                raise
            print(
                "\nTurn cancelled. You can continue the conversation.", file=sys.stderr
            )
            if terminal_status is not None:
                duration = time.monotonic() - turn_started
                terminal_status.event(
                    "cancelled", f"turn {turn_number} after {duration:.1f}s"
                )
        except ModelError as exc:
            if session is not None:
                session.record_turn("error")
            print(f"Error: {exc}", file=sys.stderr)
            exit_code = 1
            if terminal_status is not None:
                duration = time.monotonic() - turn_started
                detail = f"turn {turn_number} after {duration:.1f}s"
                terminal_status.event("error", detail)
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
            if terminal_status is not None:
                result = "truncated" if response.truncated else "completed"
                terminal_status.event(
                    result,
                    f"turn {turn_number} in {time.monotonic() - turn_started:.1f}s",
                )
        if not interactive:
            return exit_code
        if terminal_status is not None:
            terminal_status.ready()
        prompt = None


def _auth_command(argv: list[str]) -> int:
    command = argv[0]
    parser = argparse.ArgumentParser(
        prog=f"mclaude {command}",
        description=(
            "Configure and save user-level API credentials."
            if command == "login"
            else "Remove saved API credentials (sessions are kept)."
        ),
    )
    if command == "login":
        parser.add_argument("--model", help="Default model ID in the login prompt")
        parser.add_argument("--base-url", help="Default API URL in the login prompt")
    args = parser.parse_args(argv[1:])
    store = ConfigStore()
    try:
        if command == "login":
            login(store, model=args.model, base_url=args.base_url)
        else:
            removed = store.logout()
            print(
                "Logged out: saved login configuration removed."
                if removed
                else "Already logged out: no saved login configuration.",
                file=sys.stderr,
            )
            print(
                "Environment variables and explicitly supplied --env-file "
                "credentials remain independent of saved login.",
                file=sys.stderr,
            )
    except ConfigurationError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nLogin cancelled.", file=sys.stderr)
        return 130
    return 0


def main(argv: list[str] | None = None) -> int:
    """Parse command-line options and run the application."""
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in {"login", "logout"}:
        return _auth_command(argv)
    parser = argparse.ArgumentParser(
        prog="mclaude",
        description="MClaude: a local-first Python coding agent for the terminal.",
        epilog=(
            "Run 'mclaude login' to configure API access; 'mclaude logout' "
            "removes saved credentials. Environment variables override saved login."
        ),
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {version('mclaude')}"
    )
    parser.add_argument("prompt", nargs="?", help="Question to send to the model")
    parser.add_argument(
        "--read-workers",
        type=int,
        default=4,
        help="Concurrent local read tools (1-16; default: 4)",
    )
    parser.add_argument(
        "--subagent-budget",
        type=int,
        default=8,
        help="Total delegated model requests per turn (0-32; default: 8)",
    )
    parser.add_argument(
        "--mcp-config", type=Path, help="Enable stdio MCP servers from JSON"
    )
    parser.add_argument(
        "--hooks", type=Path, help="Explicitly enable hooks from a JSON file"
    )
    parser.add_argument(
        "--list-skills", action="store_true", help="List available skills"
    )
    parser.add_argument(
        "--plan", action="store_true", help="Analyze using only read-only tools"
    )
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
        "--plain",
        action="store_true",
        help="Disable the interactive terminal status page",
    )
    parser.add_argument(
        "--show-instructions",
        action="store_true",
        help="Show project instruction sources for the current workspace and exit",
    )
    parser.add_argument("--model", help="Model ID (overrides ANTHROPIC_MODEL)")
    parser.add_argument(
        "--env-file", type=Path, help="Explicitly load a dotenv file (optional)"
    )
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
        "--request-retries",
        type=int,
        default=2,
        help="Retries for transient model request failures (default: 2)",
    )
    parser.add_argument(
        "--retry-delay",
        type=float,
        default=0.5,
        help="Initial retry backoff in seconds (default: 0.5)",
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
    if args.list_skills:
        skills = SkillCatalog(Path.cwd())
        print(skills.summary() or "No skills discovered.")
        for warning in skills.warnings:
            print(warning, file=sys.stderr)
        return 0
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
    if not 0 <= args.subagent_budget <= 32:
        parser.error("--subagent-budget must be between 0 and 32.")
    if not 1 <= args.read_workers <= 16:
        parser.error("--read-workers must be between 1 and 16.")
    if args.context_budget <= args.max_tokens:
        parser.error("--context-budget must be greater than --max-tokens.")
    if args.env_file is not None:
        if not args.env_file.is_file():
            parser.error("--env-file must point to an existing file.")
        try:
            load_dotenv(args.env_file, override=False)
        except (OSError, UnicodeError):
            parser.error("Cannot read --env-file.")
    request_options = {
        "max_tokens": args.max_tokens,
        "timeout": args.timeout,
        "request_retries": args.request_retries,
        "retry_delay": args.retry_delay,
    }
    try:
        # Validate command options before prompting for or saving credentials.
        ModelConfig(
            api_key="validation",
            model=args.model if args.model is not None else "validation",
            **request_options,
        )
        store = ConfigStore()
        saved = store.load()
        api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip() or saved.get(
            "api_key", ""
        )
        model = (
            args.model
            if args.model is not None
            else os.environ.get("ANTHROPIC_MODEL", "").strip() or saved.get("model", "")
        )
        if not api_key.strip() or not model.strip():
            print("API login configuration is incomplete.", file=sys.stderr)
            saved = login(
                store,
                model=model.strip() or None,
                base_url=os.environ.get(
                    "ANTHROPIC_BASE_URL", saved.get("base_url", "")
                ),
            )
        config = ModelConfig.from_env(
            model=args.model,
            saved=saved,
            **request_options,
        )
    except ConfigurationError as exc:
        parser.error(str(exc))
    except KeyboardInterrupt:
        print("\nLogin cancelled.", file=sys.stderr)
        return 130
    session: Session | None = None
    session_kind = "temporary"
    terminal_status: TerminalStatus | None = None
    recovery_warning: str | None = None
    try:
        hooks = HookRunner.from_file(args.hooks) if args.hooks else None
        mcp = (
            MCPRegistry.from_file(args.mcp_config, Path.cwd())
            if args.mcp_config
            else None
        )
    except (HookError, MCPError) as exc:
        parser.error(str(exc))
    try:
        if interactive and not args.no_persist:
            store = SessionStore()
            workspace = Path.cwd()
            if args.resume is not None:
                session = store.resume(args.resume, workspace)
                session_kind = "resumed"
            elif args.continue_session:
                session = store.continue_latest(workspace)
                session_kind = "resumed"
            else:
                session = store.create(workspace, config.model)
                session_kind = "new"
            status_enabled = not args.plain and supports_status_view()
            if not status_enabled:
                label = "Resumed session" if session_kind == "resumed" else "Session"
                print(f"{label}: {session.id}", file=sys.stderr)
            recovery_warning = session.recovery_warning
        elif interactive:
            status_enabled = not args.plain and supports_status_view()
        else:
            status_enabled = False
        if interactive:
            terminal_status = TerminalStatus(
                enabled=status_enabled,
                workspace=Path.cwd().resolve(),
                model=config.model,
                session_id=session.id if session is not None else None,
                session_kind=session_kind,
                planning=args.plan,
                context_budget=args.context_budget,
                max_tokens=config.max_tokens,
                history=session.history if session is not None else [],
                version=version("mclaude"),
                hooks_enabled=hooks is not None,
                mcp_enabled=mcp is not None,
                subagent_budget=args.subagent_budget,
                read_workers=args.read_workers,
                tasks=session.task_board.tasks if session is not None else [],
            )
            terminal_status.open()
            if recovery_warning:
                print(
                    f"Session recovery warning: {recovery_warning}",
                    file=sys.stderr,
                )
                terminal_status.event("warning", "session recovery was required")
        try:
            return _run_conversation(
                args.prompt,
                config,
                interactive=interactive,
                max_iterations=args.max_iterations,
                context_budget_tokens=args.context_budget,
                session=session,
                planning=args.plan,
                hooks=hooks,
                mcp=mcp,
                subagent_budget=args.subagent_budget,
                read_workers=args.read_workers,
                terminal_status=terminal_status,
            )
        except KeyboardInterrupt:
            print("\nRequest interrupted.", file=sys.stderr)
            return 130
    except SessionError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    finally:
        if mcp is not None:
            mcp.close()
        if session is not None:
            session.close()
