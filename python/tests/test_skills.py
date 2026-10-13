"""Skill bodies do not enter context during discovery."""

from mclaude.agent import run_agent
from mclaude.config import ModelConfig
from mclaude.provider import ModelResponse, TextBlock, ToolUseBlock
from mclaude.skills import SkillCatalog


def write_skill(root, name="inspect", body="BODY_ONLY_ON_DEMAND"):
    path = root / ".mclaude" / "skills" / name / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\nname: {name}\ndescription: Inspect code\n---\n{body}", encoding="utf-8"
    )
    return path


def test_discovery_and_explicit_load(tmp_path):
    write_skill(tmp_path)
    catalog = SkillCatalog(tmp_path)
    assert catalog.summary() == "- inspect: Inspect code"
    assert "BODY_ONLY_ON_DEMAND" in catalog.load({"name": "inspect"}).content
    assert catalog.load({"name": "../outside"}).is_error


def test_invalid_changed_and_oversized_skills(tmp_path):
    path = write_skill(tmp_path)
    catalog = SkillCatalog(tmp_path)
    path.write_text("not frontmatter", encoding="utf-8")
    assert catalog.load({"name": "inspect"}).is_error
    assert SkillCatalog(tmp_path).warnings
    write_skill(tmp_path, body="a" * 100_001)
    assert SkillCatalog(tmp_path).load({"name": "inspect"}).is_error


def test_agent_injects_metadata_then_requested_body(tmp_path):
    write_skill(tmp_path)
    write_skill(tmp_path, "unused", "UNUSED_BODY")
    captured = []
    responses = iter(
        [
            ModelResponse(
                (ToolUseBlock("skill", "load_skill", {"name": "inspect"}),), "tool_use"
            ),
            ModelResponse((TextBlock("done"),), "end_turn"),
        ]
    )

    def request(messages, config, **kwargs):
        captured.append((repr(messages), kwargs["system"]))
        return next(responses)

    run_agent(
        "inspect",
        ModelConfig(api_key="test", model="test"),
        workspace=tmp_path,
        request=request,
        planning=True,
    )
    assert "BODY_ONLY_ON_DEMAND" not in repr(captured[0])
    assert "BODY_ONLY_ON_DEMAND" in captured[1][0]
    assert "UNUSED_BODY" not in repr(captured)
