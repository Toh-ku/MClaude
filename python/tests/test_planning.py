"""Planning restrictions cannot be overridden by an approving policy."""

from mclaude.agent import run_agent
from mclaude.config import ModelConfig
from mclaude.permissions import PermissionAction, PermissionDecision, PermissionGate
from mclaude.provider import ModelResponse, TextBlock, ToolUseBlock
from mclaude.tasks import TaskBoard


def test_planning_restricts_definitions_and_execution(tmp_path):
    (tmp_path / "note").write_text("readable")
    calls = [
        ToolUseBlock("read", "read_file", {"path": "note"}),
        ToolUseBlock("write", "create_file", {"path": "new", "content": "bad"}),
        ToolUseBlock("cmd", "run_command", {"command": "echo bad"}),
        ToolUseBlock("tasks", "update_tasks", {"tasks": []}),
        ToolUseBlock("restore", "restore_edit_checkpoint", {"checkpoint_id": "x"}),
    ]
    responses = iter(
        [
            ModelResponse(tuple(calls), "tool_use"),
            ModelResponse((TextBlock("plan"),), "end_turn"),
        ]
    )
    seen = []
    approved = []

    def request(messages, config, **kwargs):
        seen.append(kwargs)
        return next(responses)

    def policy(request):
        approved.append(request.tool_name)
        return PermissionDecision(PermissionAction.ALLOW, "allow everything")

    history = []
    board = TaskBoard()
    run_agent(
        "plan",
        ModelConfig(api_key="test", model="test"),
        workspace=tmp_path,
        planning=True,
        task_board=board,
        history=history,
        request=request,
        permission_gate=PermissionGate(policy=policy),
    )
    assert approved == ["read_file"]
    assert history[2]["content"][0]["content"] == "readable"
    assert all(item["is_error"] for item in history[2]["content"][1:])
    assert not (tmp_path / "new").exists()
    assert "run_command" not in {tool["name"] for tool in seen[0]["tools"]}
    assert "Planning mode" in seen[0]["system"]
