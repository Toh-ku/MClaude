"""Smoke tests for the installed command-line interface."""

import subprocess
import sys
from importlib.metadata import version

import pytest


@pytest.mark.parametrize("args", [[], ["--help"]])
def test_cli_shows_help(args: list[str]) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "mclaude", *args],
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
