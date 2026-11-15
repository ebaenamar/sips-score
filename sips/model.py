"""Harness-neutral event model. Every adapter turns its log format into these objects.

This mirrors SPEC.md: a session is an ordered list of human turns and agent actions.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class AgentAction:
    """One thing the agent did: a tool call or a text reply."""
    kind: str                      # "tool" | "text"
    tool: str = ""                 # tool name for kind == "tool"
    target: str = ""               # file path or command the tool acted on, if known
    ts: Optional[datetime] = None


@dataclass
class HumanTurn:
    """One human intervention: a prompt, an interruption or a refused tool call."""
    id: str
    text: str
    kind: str = "prompt"           # "prompt" | "interrupt" | "denial"
    ts: Optional[datetime] = None
    # filled by the analyzer
    actions_after: list[AgentAction] = field(default_factory=list)


@dataclass
class Session:
    id: str
    harness: str                   # "claude-code" | "codex" | "generic"
    project: str
    turns: list[HumanTurn] = field(default_factory=list)
    start: Optional[datetime] = None


def parse_ts(value) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
