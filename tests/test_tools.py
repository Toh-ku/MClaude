"""Tests for the bounded workspace file reader."""

from pathlib import Path

from mclaude.tools import create_file, find_files, read_file, replace_text, search_text


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


def test_create_file_creates_parents_and_returns_diff(tmp_path: Path) -> None:
    result = create_file({"path": "new/example.py", "content": "one\ntwo\n"}, tmp_path)

    assert (tmp_path / "new" / "example.py").read_text(encoding="utf-8") == (
        "one\ntwo\n"
    )
    assert result.is_error is False
    assert "Created new/example.py" in result.content
    assert "--- /dev/null" in result.content
    assert "+++ b/new/example.py" in result.content
    assert "+one" in result.content


def test_create_file_refuses_overwrite_and_outside_paths(tmp_path: Path) -> None:
    existing = tmp_path / "existing.txt"
    existing.write_text("keep", encoding="utf-8")

    overwrite = create_file({"path": "existing.txt", "content": "replace"}, tmp_path)
    outside = create_file({"path": "../outside.txt", "content": "no"}, tmp_path)

    assert overwrite.is_error is True
    assert "refusing to overwrite" in overwrite.content
    assert existing.read_text(encoding="utf-8") == "keep"
    assert outside.is_error is True
    assert not (tmp_path.parent / "outside.txt").exists()


def test_replace_text_updates_unique_match_and_returns_diff(tmp_path: Path) -> None:
    target = tmp_path / "module.py"
    target.write_text("before\nvalue = 1\nafter\n", encoding="utf-8", newline="")

    result = replace_text(
        {"path": "module.py", "old_text": "value = 1", "new_text": "value = 2"},
        tmp_path,
    )

    assert target.read_text(encoding="utf-8") == "before\nvalue = 2\nafter\n"
    assert result.is_error is False
    assert "Updated module.py" in result.content
    assert "--- a/module.py" in result.content
    assert "-value = 1" in result.content
    assert "+value = 2" in result.content


def test_replace_text_rejects_stale_or_ambiguous_match(tmp_path: Path) -> None:
    target = tmp_path / "module.py"
    target.write_text("same\nsame\n", encoding="utf-8")

    stale = replace_text(
        {"path": "module.py", "old_text": "old value", "new_text": "new value"},
        tmp_path,
    )
    ambiguous = replace_text(
        {"path": "module.py", "old_text": "same", "new_text": "changed"},
        tmp_path,
    )

    assert stale.is_error is True
    assert "may have changed" in stale.content
    assert ambiguous.is_error is True
    assert "matched 2 times" in ambiguous.content
    assert target.read_text(encoding="utf-8") == "same\nsame\n"


def test_file_edit_tools_validate_inputs_and_limits(tmp_path: Path) -> None:
    target = tmp_path / "large.txt"
    target.write_text("abcdef", encoding="utf-8")

    results = [
        create_file([], tmp_path),
        create_file({"path": "x"}, tmp_path),
        replace_text({}, tmp_path),
        replace_text(
            {"path": "large.txt", "old_text": "a", "new_text": "b"},
            tmp_path,
            max_file_bytes=3,
        ),
    ]

    assert all(result.is_error for result in results)
    assert "edit limit" in results[-1].content
    assert target.read_text(encoding="utf-8") == "abcdef"
