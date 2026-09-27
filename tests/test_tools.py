"""Tests for the bounded workspace file reader."""

from pathlib import Path

from mclaude.tools import find_files, read_file, search_text


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


def test_find_files_uses_globs_and_skips_git_metadata(tmp_path: Path) -> None:
    (tmp_path / "root.py").write_text("", encoding="utf-8")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config.py").write_text("", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "code.py").write_text("", encoding="utf-8")

    python_files = find_files({"pattern": "*.py"}, tmp_path)

    assert python_files.content.splitlines() == ["root.py", "sub/code.py"]


def test_workspace_search_does_not_apply_gitignore_rules(tmp_path: Path) -> None:
    (tmp_path / ".gitignore").write_text("ignored.py\n", encoding="utf-8")
    (tmp_path / "ignored.py").write_text("needle\n", encoding="utf-8")

    files = find_files({"pattern": "**/*.py"}, tmp_path)
    matches = search_text({"query": "needle"}, tmp_path)

    assert files.content == "ignored.py"
    assert matches.content == "ignored.py:1:needle"


def test_find_files_bounds_results_and_validates_input(tmp_path: Path) -> None:
    for name in ["a.txt", "b.txt", "c.txt"]:
        (tmp_path / name).write_text("", encoding="utf-8")

    result = find_files({"pattern": "*.txt"}, tmp_path, max_results=2)

    assert result.content.splitlines() == [
        "a.txt",
        "b.txt",
        "[Results truncated after 2 files.]",
    ]
    assert find_files({}, tmp_path).is_error is True


def test_search_text_returns_paths_lines_and_respects_scope(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "one.py").write_text(
        "first\nNeedle here\nneedle again\n", encoding="utf-8"
    )
    (tmp_path / "outside.py").write_text("Needle outside\n", encoding="utf-8")
    (tmp_path / "src" / "binary.dat").write_bytes(b"Needle\xff")

    result = search_text(
        {"query": "needle", "path": "src", "case_sensitive": False}, tmp_path
    )

    assert result.content.splitlines() == [
        "src/one.py:2:Needle here",
        "src/one.py:3:needle again",
    ]


def test_search_text_supports_regex_bounds_and_errors(tmp_path: Path) -> None:
    (tmp_path / "code.py").write_text("alpha1\nalpha2\nalpha3\n", encoding="utf-8")

    result = search_text(
        {"query": r"alpha\d", "is_regex": True}, tmp_path, max_results=2
    )

    assert result.content.splitlines() == [
        "code.py:1:alpha1",
        "code.py:2:alpha2",
        "[Results truncated after 2 matches.]",
    ]
    assert search_text({"query": "[", "is_regex": True}, tmp_path).is_error is True
    outside = search_text({"query": "x", "path": "../outside"}, tmp_path)
    assert outside.is_error is True
    assert "outside the workspace" in outside.content


def test_search_text_skips_oversized_files_and_truncates_lines(tmp_path: Path) -> None:
    (tmp_path / "large.txt").write_text("needle" * 10, encoding="utf-8")
    (tmp_path / "line.txt").write_text("needle and more", encoding="utf-8")

    result = search_text(
        {"query": "needle"},
        tmp_path,
        max_file_bytes=20,
        max_line_chars=6,
    )

    assert result.content == "line.txt:1:needle…"
