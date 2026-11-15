"""Readers for each harness's local logs. Add a new harness by writing one function that yields Sessions."""
from __future__ import annotations

import glob
import json
import os
import re
from pathlib import Path
from typing import Iterator

from .model import AgentAction, HumanTurn, Session, parse_ts

# Text the harness injects into the user role that a person did not type.
_INJECTED = re.compile(
    r"<system-reminder>.*?</system-reminder>|<pasted_content[^>]*>.*?</pasted_content[^>]*>|"
    r"<command-(?:name|message|args|stdout|stderr)>.*?</command-(?:name|message|args|stdout|stderr)>|"
    r"<local-command-stdout>.*?</local-command-stdout>|<environment_context>.*?</environment_context>|"
    r"<user_instructions>.*?</user_instructions>|<ide_[a-z_]+>.*?</ide_[a-z_]+>",
    re.S,
)
_SKIP_PREFIXES = ("<bash-input>", "<bash-stdout>", "<openbase", "<user-prompt-submit-hook>", "Caveat:", "This session is being continued", "<task-notification>", "[SYSTEM NOTIFICATION",
                  "Base directory for this skill", "Output token limit hit", "Tool loaded.")
_INTERRUPT = "[Request interrupted by user"
_DENIAL = re.compile(r"doesn't want to proceed with this tool use|user denied|rejected by user|was rejected", re.I)


def clean(text: str) -> str:
    return _INJECTED.sub(" ", text or "").strip()


def _human_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(x.get("text", "") for x in content if isinstance(x, dict) and x.get("type") == "text")
    return ""


def _target(inp: dict) -> str:
    for k in ("file_path", "path", "notebook_path", "command", "cmd"):
        if isinstance(inp, dict) and inp.get(k):
            return str(inp[k])[:300]
    return ""


# ---------------------------------------------------------------- Claude Code
def claude_code(root: str = "~/.claude/projects") -> Iterator[Session]:
    """Claude Code copies earlier messages into a new file when a session is resumed; each message counts once."""
    seen: set = set()
    root = os.path.expanduser(root)
    files = [root] if root.endswith(".jsonl") else glob.glob(os.path.join(root, "**", "*.jsonl"), recursive=True)
    for f in sorted(files):
        if "/subagents/" in f:
            continue
        project = Path(f).parent.name
        s = Session(id=Path(f).stem, harness="claude-code", project=project)
        current: HumanTurn | None = None
        for n, line in enumerate(Path(f).read_text(errors="ignore").splitlines()):
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if d.get("isSidechain"):
                continue
            if d.get("type") in ("user", "assistant"):
                # resumed sessions copy messages, sometimes with a new uuid: match on uuid and on time + content
                keys = [d.get("uuid"), (d.get("timestamp"), json.dumps(msg.get("content"))[:300] if (msg := d.get("message") or {}) else "")]
                if any(k in seen for k in keys if k):
                    continue
                seen.update(k for k in keys if k)
            ts = parse_ts(d.get("timestamp"))
            s.start = s.start or ts
            msg = d.get("message") or {}
            content = msg.get("content")
            if d.get("type") == "user" and not d.get("isMeta"):
                if isinstance(content, list) and any(isinstance(x, dict) and x.get("type") == "tool_result" for x in content):
                    for x in content:
                        if isinstance(x, dict) and x.get("type") == "tool_result" and _DENIAL.search(json.dumps(x.get("content", ""))[:600]):
                            current = HumanTurn(id=f"{s.id[:8]}:{n}", text="[denied a tool call]", kind="denial", ts=ts)
                            s.turns.append(current)
                    continue
                raw = _human_text(content)
                if _INTERRUPT in raw:
                    current = HumanTurn(id=f"{s.id[:8]}:{n}", text=raw.strip(), kind="interrupt", ts=ts)
                    s.turns.append(current)
                    continue
                text = clean(raw)
                if not text or text.startswith(_SKIP_PREFIXES):
                    continue
                current = HumanTurn(id=f"{s.id[:8]}:{n}", text=text, ts=ts)
                s.turns.append(current)
            elif d.get("type") == "assistant" and current is not None and isinstance(content, list):
                for x in content:
                    if not isinstance(x, dict):
                        continue
                    if x.get("type") == "tool_use":
                        current.actions_after.append(AgentAction("tool", x.get("name", ""), _target(x.get("input", {})), ts))
                    elif x.get("type") == "text" and x.get("text", "").strip():
                        current.actions_after.append(AgentAction("text", ts=ts))
        if s.turns:
            yield s


# ---------------------------------------------------------------- Codex CLI
def codex(root: str = "~/.codex/sessions") -> Iterator[Session]:
    root = os.path.expanduser(root)
    files = [root] if root.endswith(".jsonl") else glob.glob(os.path.join(root, "**", "*.jsonl"), recursive=True)
    for f in sorted(files):
        s = Session(id=Path(f).stem, harness="codex", project="")
        current: HumanTurn | None = None
        for n, line in enumerate(Path(f).read_text(errors="ignore").splitlines()):
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            p = d.get("payload") or {}
            ts = parse_ts(d.get("timestamp"))
            s.start = s.start or ts
            if d.get("type") == "session_meta":
                s.project = Path(p.get("cwd", "")).name or s.project
            if d.get("type") != "response_item":
                continue
            t = p.get("type")
            if t == "message" and p.get("role") == "user":
                text = clean(_codex_text(p.get("content")))
                if text and not text.startswith(_SKIP_PREFIXES):
                    current = HumanTurn(id=f"{s.id[-8:]}:{n}", text=text, ts=ts)
                    s.turns.append(current)
            elif current is not None and t in ("function_call", "custom_tool_call", "local_shell_call"):
                args = p.get("arguments") or p.get("input") or ""
                current.actions_after.append(AgentAction("tool", p.get("name", t), str(args)[:300], ts))
            elif current is not None and t == "message" and p.get("role") == "assistant":
                current.actions_after.append(AgentAction("text", ts=ts))
        if s.turns:
            yield s


def _codex_text(content) -> str:
    if isinstance(content, list):
        return "\n".join(x.get("text", "") for x in content if isinstance(x, dict) and x.get("type") in ("input_text", "text"))
    return content if isinstance(content, str) else ""


# ---------------------------------------------------------------- generic
def generic(path: str) -> Iterator[Session]:
    """Any harness: a JSON/JSONL file of chat messages ({"role", "content"} plus optional "tool_calls").

    Accepts one conversation per line (a list of messages or {"id", "messages"}), a single JSON list, or
    ChatGPT-style exports are out of scope for now. See SPEC.md for the native event format.
    """
    path = os.path.expanduser(path)
    text = Path(path).read_text(errors="ignore")
    try:
        data = json.loads(text)
        convs = data if isinstance(data, list) and data and isinstance(data[0], (list, dict)) and not (isinstance(data[0], dict) and "role" in data[0]) else [data]
    except json.JSONDecodeError:
        convs = [json.loads(l) for l in text.splitlines() if l.strip()]
    for i, conv in enumerate(convs):
        msgs = conv.get("messages", []) if isinstance(conv, dict) else conv
        s = Session(id=str(conv.get("id", i)) if isinstance(conv, dict) else str(i), harness="generic", project=Path(path).stem)
        current = None
        for n, m in enumerate(msgs):
            role = m.get("role")
            if role == "user":
                text = clean(_human_text(m.get("content")))
                if text:
                    current = HumanTurn(id=f"{s.id}:{n}", text=text, ts=parse_ts(m.get("timestamp")))
                    s.turns.append(current)
            elif role in ("assistant", "tool") and current is not None:
                for tc in m.get("tool_calls") or []:
                    fn = tc.get("function", tc)
                    current.actions_after.append(AgentAction("tool", fn.get("name", ""), str(fn.get("arguments", ""))[:300]))
                if role == "assistant" and m.get("content"):
                    current.actions_after.append(AgentAction("text"))
        if s.turns:
            yield s


def load(harness: str, path: str | None) -> list[Session]:
    if harness == "claude-code":
        return list(claude_code(path or "~/.claude/projects"))
    if harness == "codex":
        return list(codex(path or "~/.codex/sessions"))
    if harness == "generic":
        if not path:
            raise SystemExit("--harness generic needs a file path")
        return list(generic(path))
    # auto: everything that exists on this machine
    out = []
    if path:
        p = os.path.expanduser(path)
        if ".codex" in p:
            return list(codex(p))
        if ".claude" in p:
            return list(claude_code(p))
        return list(generic(p))
    if os.path.isdir(os.path.expanduser("~/.claude/projects")):
        out += claude_code()
    if os.path.isdir(os.path.expanduser("~/.codex/sessions")):
        out += codex()
    return out
