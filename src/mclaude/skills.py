"""Discover bounded skill metadata; load bodies only on explicit tool demand."""

import json
import re
from dataclasses import dataclass
from pathlib import Path

from mclaude.tools import ToolResult

LOAD_SKILL_DEFINITION = {
    "name": "load_skill",
    "description": "Load a discovered skill by name when its description is relevant.",
    "input_schema": {
        "type": "object",
        "properties": {"name": {"type": "string"}},
        "required": ["name"],
        "additionalProperties": False,
    },
}


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    path: Path


def _safe(path: Path, root: Path) -> bool:
    return path.resolve().is_relative_to(root) and not any(
        item.is_symlink() for item in [path, *path.parents] if item != root
    )


def _metadata(path: Path) -> tuple[str, str]:
    # This intentionally accepts a small, documented frontmatter format.
    with path.open("r", encoding="utf-8") as stream:
        if stream.readline(100).strip() != "---":
            raise ValueError("Missing skill frontmatter.")
        metadata = {}
        size = 0
        while size <= 8192:
            line = stream.readline(8193)
            size += len(line)
            if line.strip() == "---":
                break
            if not line or size > 8192:
                raise ValueError("Invalid skill frontmatter.")
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            key, sep, value = line.partition(":")
            if not sep or key not in {"name", "description"} or key in metadata:
                raise ValueError("Expected unique name and description fields.")
            value = value.strip()
            if value.startswith('"'):
                value = json.loads(value)
            elif value.startswith("'") and value.endswith("'"):
                value = value[1:-1].replace("''", "'")
            metadata[key] = value
    name, description = metadata.get("name"), metadata.get("description")
    if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", name):
        raise ValueError("Invalid skill name.")
    if not isinstance(description, str) or not 1 <= len(description) <= 500:
        raise ValueError("Invalid skill description.")
    return name, description


class SkillCatalog:
    def __init__(self, workspace: Path):
        self.workspace = workspace.resolve()
        self.skills: dict[str, Skill] = {}
        self.warnings: list[str] = []
        directory = self.workspace / ".mclaude" / "skills"
        if not _safe(directory, self.workspace) or not directory.is_dir():
            return
        for path in sorted(directory.glob("*/SKILL.md")):
            if len(self.skills) >= 100:
                self.warnings.append("Skill catalog limited to 100 entries.")
                break
            if not _safe(path, self.workspace):
                continue
            try:
                name, description = _metadata(path)
                if name in self.skills:
                    raise ValueError("Duplicate skill name.")
            except (OSError, ValueError) as exc:
                self.warnings.append(
                    f"Skipped {path.relative_to(self.workspace)}: {exc}"
                )
                continue
            self.skills[name] = Skill(name, description, path)

    def summary(self) -> str:
        return "\n".join(
            f"- {skill.name}: {skill.description}" for skill in self.skills.values()
        )

    def load(self, arguments: object) -> ToolResult:
        if (
            not isinstance(arguments, dict)
            or set(arguments) != {"name"}
            or not isinstance(arguments["name"], str)
        ):
            return ToolResult("load_skill requires a skill name.", is_error=True)
        skill = self.skills.get(arguments["name"])
        if skill is None:
            return ToolResult("Unknown skill name.", is_error=True)
        try:
            if not _safe(skill.path, self.workspace):
                raise ValueError("Skill path is no longer inside the workspace.")
            name, description = _metadata(skill.path)
            if (name, description) != (skill.name, skill.description):
                raise ValueError("Skill metadata changed; rediscover on the next turn.")
            with skill.path.open(encoding="utf-8") as stream:
                content = stream.read(100_001)
            if len(content) > 100_000:
                raise ValueError("Skill exceeds 100,000 characters.")
        except (OSError, ValueError) as exc:
            return ToolResult(f"Cannot load skill: {exc}", is_error=True)
        return ToolResult(
            f"Skill source: {skill.path.relative_to(self.workspace)}\n{content}"
        )
