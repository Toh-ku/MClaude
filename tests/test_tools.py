"""Tests for the bounded workspace file reader."""

from pathlib import Path

from mclaude.tools import read_file


def test_read_file_reads_utf8_text(tmp_path: Path) -> None:
    (tmp_path / "hello.txt").write_text("你好\nagent", encoding="utf-8")

    result = read_file({"path": "hello.txt"}, tmp_path, max_chars=100)

    assert result.content == "你好\nagent"
    assert result.is_error is False


def test_read_file_truncates_large_output(tmp_path: Path) -> None:
    (tmp_path / "large.txt").write_text("abcdefgh", encoding="utf-8")

    result = read_file({"path": "large.txt"}, tmp_path, max_chars=5)

    assert result.content.startswith("abcde\n\n[Output truncated")
    assert result.is_error is False


def test_read_file_rejects_paths_outside_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("secret", encoding="utf-8")

    result = read_file({"path": "../secret.txt"}, workspace, max_chars=100)

    assert result.is_error is True
    assert "outside the workspace" in result.content


def test_read_file_reports_invalid_inputs_and_file_errors(tmp_path: Path) -> None:
    (tmp_path / "folder").mkdir()

    results = [
        read_file([], tmp_path, max_chars=100),
        read_file({}, tmp_path, max_chars=100),
        read_file({"path": "missing.txt"}, tmp_path, max_chars=100),
        read_file({"path": "folder"}, tmp_path, max_chars=100),
    ]

    assert all(result.is_error for result in results)
    assert "must be an object" in results[0].content
    assert "requires" in results[1].content
    assert "not found" in results[2].content
    assert "not a file" in results[3].content


def test_read_file_reports_non_utf8_file(tmp_path: Path) -> None:
    (tmp_path / "binary.dat").write_bytes(b"\xff\xfe")

    result = read_file({"path": "binary.dat"}, tmp_path, max_chars=100)

    assert result.is_error is True
    assert "UnicodeDecodeError" in result.content
