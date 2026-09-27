"""Smoke tests for the installed command-line interface."""

import subprocess
import sys
from importlib.metadata import version

import pytest

from mclaude import cli
from mclaude.provider import ModelError, ModelResponse, TextBlock, ToolUseBlock


@pytest.mark.parametrize("args", [[], ["--help"]])
def test_cli_shows_help(args: list[str], tmp_path) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "mclaude", *args],
        input="",
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0
    assert "usage: mclaude" in result.stdout
    assert "--version" in result.stdout
    assert result.stderr == ""


def test_cli_shows_installed_version() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "mclaude", "--version"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == f"mclaude {version('mclaude')}"


def test_cli_rejects_unknown_options() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "mclaude", "--unknown"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 2
    assert "unrecognized arguments: --unknown" in result.stderr


@pytest.fixture
def conversation(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-secret")
    monkeypatch.setenv("ANTHROPIC_MODEL", "test-model")
    monkeypatch.setenv("MCLAUDE_STATE_DIR", str(tmp_path / ".state"))
    calls = []

    def request(messages, config, *, tools, on_text=None):
        calls.append(messages.copy())
        return ModelResponse((TextBlock(f"Answer {len(calls)}"),), "end_turn")

    monkeypatch.setattr("mclaude.agent.create_message", request)
    return calls


def enter_lines(monkeypatch, lines):
    entries = iter(lines)

    def read_line():
        try:
            line = next(entries)
        except StopIteration:
            raise EOFError from None
        if isinstance(line, BaseException):
            raise line
        return line

    monkeypatch.setattr("builtins.input", read_line)


@pytest.mark.parametrize("args", [[], ["--interactive"], ["-i", "First question"]])
def test_interactive_followup_preserves_history(
    args, conversation, monkeypatch, capsys
):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: not args)
    lines = ["First question"] if "First question" not in args else []
    enter_lines(monkeypatch, [*lines, "  ", "Actually, use Chinese", "/exit"])

    assert cli.main(args) == 0
    assert len(conversation) == 2
    assert conversation[1] == [
        {"role": "user", "content": "First question"},
        {"role": "assistant", "content": [{"type": "text", "text": "Answer 1"}]},
        {"role": "user", "content": "Actually, use Chinese"},
    ]
    captured = capsys.readouterr()
    assert captured.out == "Answer 1\nAnswer 2\n"
    assert "You> " in captured.err


@pytest.mark.parametrize("lines", [[], ["  "], [" /quit "], ["/exit"]])
def test_interactive_empty_session_exits_without_request(
    lines, conversation, monkeypatch
):
    enter_lines(monkeypatch, lines)
    assert cli.main(["-i"]) == 0
    assert conversation == []


def test_interactive_request_failure_allows_followup(conversation, monkeypatch, capsys):
    def request(messages, config, *, tools, on_text=None):
        conversation.append(messages.copy())
        if len(conversation) == 1:
            raise ModelError("Request timed out")
        return ModelResponse((TextBlock("Recovered"),), "end_turn")

    monkeypatch.setattr("mclaude.agent.create_message", request)
    enter_lines(monkeypatch, ["First", "Try again"])

    assert cli.main(["-i"]) == 1
    assert conversation[1] == [
        {"role": "user", "content": "First"},
        {"role": "user", "content": "Try again"},
    ]
    captured = capsys.readouterr()
    assert captured.out == "Recovered\n"
    assert "Request timed out" in captured.err
    assert "Traceback" not in captured.err


def test_interactive_truncation_allows_followup(conversation, monkeypatch, capsys):
    def request(messages, config, *, tools, on_text=None):
        conversation.append(messages.copy())
        return ModelResponse(
            (TextBlock("Partial" if len(conversation) == 1 else "Finished"),),
            "max_tokens" if len(conversation) == 1 else "end_turn",
        )

    monkeypatch.setattr("mclaude.agent.create_message", request)
    enter_lines(monkeypatch, ["First", "Continue"])

    assert cli.main(["-i"]) == 1
    assert conversation[1][1]["content"] == [{"type": "text", "text": "Partial"}]
    captured = capsys.readouterr()
    assert captured.out == "Partial\nFinished\n"
    assert "Output truncated" in captured.err


@pytest.mark.parametrize("during_request", [False, True])
def test_interactive_interrupt_behavior(
    conversation, monkeypatch, capsys, during_request
):
    if during_request:

        def interrupt(*args, **kwargs):
            raise KeyboardInterrupt

        monkeypatch.setattr("mclaude.agent.create_message", interrupt)
        enter_lines(monkeypatch, ["Hello"])
    else:
        enter_lines(monkeypatch, [KeyboardInterrupt()])

    assert cli.main(["-i"]) == (0 if during_request else 130)
    assert (
        "Turn cancelled" if during_request else "interrupted"
    ) in capsys.readouterr().err


def test_cancelled_stream_can_be_followed_by_another_turn(
    conversation, monkeypatch, capsys
):
    def request(messages, config, *, tools, on_text):
        conversation.append(messages.copy())
        if len(conversation) == 1:
            on_text("Partial answer")
            raise KeyboardInterrupt
        on_text("New answer")
        return ModelResponse((TextBlock("New answer"),), "end_turn")

    monkeypatch.setattr("mclaude.agent.create_message", request)
    enter_lines(monkeypatch, ["First", "Do something else", "/quit"])
    assert cli.main(["-i"]) == 0
    assert conversation[1][1] == {
        "role": "assistant",
        "content": [{"type": "text", "text": "Partial answer"}],
    }
    assert conversation[1][-1]["content"] == "Do something else"
    captured = capsys.readouterr()
    assert captured.out == "Partial answer\nNew answer\n"
    assert "Turn cancelled" in captured.err


def test_single_turn_interrupt_returns_130(conversation, monkeypatch, capsys):
    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr("mclaude.agent.create_message", interrupt)
    assert cli.main(["Hello"]) == 130
    assert "interrupted" in capsys.readouterr().err


def test_interactive_permission_answer_is_not_a_turn(
    conversation, monkeypatch, tmp_path, capsys
):
    responses = iter(
        [
            ModelResponse(
                (
                    ToolUseBlock(
                        "create", "create_file", {"path": "new.txt", "content": "hi"}
                    ),
                ),
                "tool_use",
            ),
            ModelResponse((TextBlock("Created"),), "end_turn"),
            ModelResponse((TextBlock("Followup"),), "end_turn"),
        ]
    )

    def request(messages, config, *, tools, on_text=None):
        conversation.append(messages.copy())
        return next(responses)

    monkeypatch.setattr("mclaude.agent.create_message", request)
    enter_lines(monkeypatch, ["Create a file", "y", "Summarize it", "/quit"])

    assert cli.main(["-i"]) == 0
    assert (tmp_path / "new.txt").read_text() == "hi"
    assert [
        m["content"] for m in conversation[-1] if isinstance(m["content"], str)
    ] == [
        "Create a file",
        "Summarize it",
    ]
    assert "Allow this tool call?" in capsys.readouterr().err


def test_positional_prompt_stays_single_turn_in_terminal(conversation, monkeypatch):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)

    def unexpected_input():
        pytest.fail("Single-turn invocation must not read another prompt")

    monkeypatch.setattr("builtins.input", unexpected_input)
    assert cli.main(["One task"]) == 0
    assert len(conversation) == 1


def test_continue_restores_latest_workspace_session(conversation, monkeypatch, capsys):
    enter_lines(monkeypatch, ["/exit"])
    assert cli.main(["-i", "First question"]) == 0

    enter_lines(monkeypatch, ["Follow up", "/exit"])
    assert cli.main(["--continue"]) == 0

    assert conversation[1] == [
        {"role": "user", "content": "First question"},
        {"role": "assistant", "content": [{"type": "text", "text": "Answer 1"}]},
        {"role": "user", "content": "Follow up"},
    ]
    captured = capsys.readouterr()
    assert "Session:" in captured.err
    assert "Resumed session:" in captured.err


def test_resume_specific_session(conversation, monkeypatch, capsys):
    enter_lines(monkeypatch, ["/exit"])
    assert cli.main(["-i", "Remember me"]) == 0
    first_run = capsys.readouterr()
    session_id = first_run.err.split("Session: ", 1)[1].splitlines()[0]

    enter_lines(monkeypatch, ["Continue", "/exit"])
    assert cli.main(["--resume", session_id]) == 0
    assert conversation[1][0]["content"] == "Remember me"


def test_no_persist_leaves_no_saved_session(conversation, monkeypatch, tmp_path):
    enter_lines(monkeypatch, ["/exit"])
    assert cli.main(["-i", "Private", "--no-persist"]) == 0
    assert not (tmp_path / ".state").exists()


def test_single_turn_does_not_create_session(conversation, tmp_path):
    assert cli.main(["One task"]) == 0
    assert not (tmp_path / ".state").exists()
