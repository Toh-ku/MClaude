"""File-scoped rules must reach the model before an edit is allowed."""

import pytest

from mclaude.agent import run_agent
from mclaude.checkpoints import CheckpointStore
from mclaude.config import ModelConfig
from mclaude.context import ContextError, instructions_for_path
from mclaude.permissions import PermissionGate
from mclaude.provider import ModelResponse, TextBlock, ToolUseBlock


def test_checkpoint_restore_reviews_new_rules_first(tmp_path):
    (tmp_path / "src").mkdir()
    target = tmp_path / "src" / "module.py"
    target.write_text("after")
    checkpoints = CheckpointStore(tmp_path)
    checkpoint_id = checkpoints.save(target, b"before", b"after")
    (tmp_path / "src" / "AGENTS.md").write_text("Review recovery")
    requests = []

    def request(*_, **kwargs):
        requests.append(kwargs)
        if len(requests) == 2:
            assert "Review recovery" in kwargs["system"]
            assert target.read_text() == "after"
        if len(requests) <= 2:
            return ModelResponse(
                (
                    ToolUseBlock(
                        f"restore-{len(requests)}",
                        "restore_edit_checkpoint",
                        {"checkpoint_id": checkpoint_id.upper()},
                    ),
                ),
                "tool_use",
            )
        return ModelResponse((TextBlock("done"),), "end_turn")

    run_agent(
        "restore",
        ModelConfig(api_key="test", model="test"),
        workspace=tmp_path,
        request=request,
        checkpoint_store=checkpoints,
        permission_gate=PermissionGate(prompt=lambda *_: True),
    )
    assert target.read_text() == "before"


def test_removed_rule_during_approval_also_blocks_edit(tmp_path):
    rule = tmp_path / "AGENTS.md"
    rule.write_text("Old rule")
    target = tmp_path / "module.py"
    target.write_text("before")
    responses = iter(
        [
            ModelResponse(
                (
                    ToolUseBlock(
                        "edit",
                        "replace_text",
                        {
                            "path": "module.py",
                            "old_text": "before",
                            "new_text": "after",
                        },
                    ),
                ),
                "tool_use",
            ),
            ModelResponse((TextBlock("done"),), "end_turn"),
        ]
    )
    history = []
    run_agent(
        "edit",
        ModelConfig(api_key="test", model="test"),
        workspace=tmp_path,
        request=lambda *_, **__: next(responses),
        history=history,
        permission_gate=PermissionGate(prompt=lambda *_: rule.unlink() or True),
    )
    assert target.read_text() == "before"
    assert "Previously supplied rule removed" in str(history)


def test_new_file_rules_follow_ancestors_and_isolate_siblings(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "other").mkdir()
    for name, text in [("AGENTS.md", "root"), ("CLAUDE.md", "root later")]:
        (tmp_path / name).write_text(text)
    (tmp_path / "src" / "AGENTS.md").write_text("src only")
    (tmp_path / "src" / "CLAUDE.md").write_text("src later")
    (tmp_path / "other" / "AGENTS.md").write_text("sibling only")
    rules = instructions_for_path(tmp_path, "src/new/deep/module.py")
    assert [s.content for s in rules.sources] == [
        "root",
        "root later",
        "src only",
        "src later",
    ]
    assert "sibling only" not in rules.system_prompt()
    with pytest.raises(ContextError, match="outside"):
        instructions_for_path(tmp_path, "../outside.py")


def test_nested_read_loads_rules_into_next_request(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "AGENTS.md").write_text("Use local tests")
    (tmp_path / "src" / "module.py").write_text("value")
    requests = []

    def request(messages, *_, **kwargs):
        requests.append((messages.copy(), kwargs.get("system", "")))
        if len(requests) == 1:
            return ModelResponse(
                (ToolUseBlock("read", "read_file", {"path": "src/module.py"}),),
                "tool_use",
            )
        return ModelResponse((TextBlock("done"),), "end_turn")

    run_agent(
        "read",
        ModelConfig(api_key="test", model="test"),
        workspace=tmp_path,
        request=request,
    )
    assert "Use local tests" not in requests[0][1]
    assert "Use local tests" in requests[1][1]
    assert "Use local tests" in str(requests[1][0])


def test_first_nested_edit_is_deferred_before_approval(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "AGENTS.md").write_text("Keep compatibility")
    target = tmp_path / "src" / "module.py"
    target.write_text("before")
    approvals = []
    requests = []

    def request(messages, *_, **kwargs):
        requests.append(kwargs)
        if len(requests) == 2:
            assert target.read_text() == "before"
            assert not approvals
            assert "Keep compatibility" in kwargs["system"]
        if len(requests) <= 2:
            return ModelResponse(
                (
                    ToolUseBlock(
                        f"edit-{len(requests)}",
                        "replace_text",
                        {
                            "path": "src/module.py",
                            "old_text": "before",
                            "new_text": "after",
                        },
                    ),
                ),
                "tool_use",
            )
        return ModelResponse((TextBlock("done"),), "end_turn")

    run_agent(
        "edit",
        ModelConfig(api_key="test", model="test"),
        workspace=tmp_path,
        request=request,
        permission_gate=PermissionGate(
            prompt=lambda item, *_: approvals.append(item) or True
        ),
    )
    assert len(approvals) == 1
    assert target.read_text() == "after"


def test_read_then_edit_in_one_response_cannot_bypass_rule_review(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "AGENTS.md").write_text("Nested rule")
    target = tmp_path / "src" / "module.py"
    target.write_text("before")
    responses = iter(
        [
            ModelResponse(
                (
                    ToolUseBlock("read", "read_file", {"path": "src/module.py"}),
                    ToolUseBlock(
                        "edit",
                        "replace_text",
                        {
                            "path": "src/module.py",
                            "old_text": "before",
                            "new_text": "after",
                        },
                    ),
                ),
                "tool_use",
            ),
            ModelResponse((TextBlock("Rules reviewed"),), "end_turn"),
        ]
    )
    run_agent(
        "read then edit",
        ModelConfig(api_key="test", model="test"),
        workspace=tmp_path,
        request=lambda *_, **__: next(responses),
        permission_gate=PermissionGate(prompt=lambda *_: True),
    )
    assert target.read_text() == "before"


def test_batch_edit_loads_all_scopes_before_any_file_changes(tmp_path):
    for directory in ["a", "b"]:
        (tmp_path / directory).mkdir()
        (tmp_path / directory / "AGENTS.md").write_text(f"{directory} only rule")
        (tmp_path / directory / "module.py").write_text("before")
    edits = [
        {
            "kind": "replace",
            "path": f"{directory}/module.py",
            "old_text": "before",
            "new_text": "after",
        }
        for directory in ["a", "b"]
    ]
    requests = []

    def request(*_, **kwargs):
        requests.append(kwargs)
        if len(requests) == 2:
            for directory in ["a", "b"]:
                assert (tmp_path / directory / "module.py").read_text() == "before"
                assert f"{directory} only rule" in kwargs["system"]
            assert "sibling scopes never override" in kwargs["system"]
        if len(requests) <= 2:
            return ModelResponse(
                (
                    ToolUseBlock(
                        f"batch-{len(requests)}", "apply_edits", {"edits": edits}
                    ),
                ),
                "tool_use",
            )
        return ModelResponse((TextBlock("done"),), "end_turn")

    run_agent(
        "edit both",
        ModelConfig(api_key="test", model="test"),
        workspace=tmp_path,
        request=request,
        permission_gate=PermissionGate(prompt=lambda *_: True),
    )
    for directory in ["a", "b"]:
        assert (tmp_path / directory / "module.py").read_text() == "after"


def test_rules_changed_during_approval_block_edit(tmp_path):
    rule = tmp_path / "AGENTS.md"
    rule.write_text("Old rule")
    target = tmp_path / "module.py"
    target.write_text("before")
    requests = []

    def request(*_, **kwargs):
        requests.append(kwargs)
        if len(requests) == 1:
            return ModelResponse(
                (
                    ToolUseBlock(
                        "edit",
                        "replace_text",
                        {
                            "path": "module.py",
                            "old_text": "before",
                            "new_text": "after",
                        },
                    ),
                ),
                "tool_use",
            )
        assert "Changed rule" in kwargs["system"]
        return ModelResponse((TextBlock("done"),), "end_turn")

    def approve(*_):
        rule.write_text("Changed rule")
        return True

    run_agent(
        "edit",
        ModelConfig(api_key="test", model="test"),
        workspace=tmp_path,
        request=request,
        permission_gate=PermissionGate(prompt=approve),
    )
    assert target.read_text() == "before"
