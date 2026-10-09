from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import yaml

PROMPT_DIR = Path(__file__).parent / "prompts" / "v1"


@dataclass(frozen=True)
class AgentConfig:
    id: str
    label: str
    description: str
    knowledge_collections: list[str]
    tools: list[str]
    support_queue: str
    prompt_version: str
    keywords: dict[str, float] = field(default_factory=dict)

    @property
    def instructions(self) -> str:
        path = PROMPT_DIR / f"{self.id}.md"
        return path.read_text(encoding="utf-8") if path.exists() else ""


@lru_cache
def load_agents() -> dict[str, AgentConfig]:
    raw = yaml.safe_load((Path(__file__).parent / "agents.yaml").read_text(encoding="utf-8"))
    return {k: AgentConfig(id=k, **v) for k, v in raw["agents"].items()}


def get_agent(agent_id: str) -> AgentConfig:
    return load_agents()[agent_id]


def load_prompt(name: str) -> str:
    return (PROMPT_DIR / f"{name}.md").read_text(encoding="utf-8")
