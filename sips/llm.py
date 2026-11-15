"""Optional: classify turns with the agent CLI you already use (claude, codex, or any command reading stdin).

No extra API key: `claude -p` / `codex exec` run on your existing account. Turns are redacted before sending.
The rules in classify.py stay the default because they are free, instant and fully explainable.
"""
from __future__ import annotations

import json
import re
import shlex
import subprocess

from .redact import redact

PROMPT = """You label how a person intervenes in their work with an AI coding/writing agent, using the four tiers of the
Sustainable Intervention Points framework (Baena, Torres & Chíncaro, BiAlign@CHI'26). Leverage = how many future agent
decisions one human action changes.

Tier 1 Strategic control: defines goals, constraints, specs, plans, architecture or rules before/around the work
  ("never touch the migrations folder", "plan it in phases with a test per piece before coding").
Tier 2 Calibration: gives information the agent lacked or teaches it: context, facts, data, links, error reports,
  examples, corrections that say why ("the config lives in /etc/app", "the build fails with a timeout", "make it read like: ...").
Tier 3 Informed oversight: questions or reviews outputs, spots patterns across them ("does this handle empty input?",
  "check the other two functions as well", "you keep adding unused imports").
Tier 4 Mechanical review: approves, continues, retries, or asks for one small concrete edit or deliverable with no new
  information ("ok", "go on", "rerun it", "rename the variable to count").

Also give the feedback type: generalising, example, diagnostic, criteria, context, verify-request, question, directive,
pattern-report, outcome-only, restated, acknowledge, instruction.

Answer ONLY with a JSON list, one object per turn: {"i": <number>, "tier": 1-4, "feedback": "<type>", "why": "<max 15 words>"}.

Turns:
"""


def _run(backend: str, prompt: str, timeout: int = 300) -> str:
    if backend == "claude":
        cmd = ["claude", "-p", "--output-format", "text"]
    elif backend == "codex":
        cmd = ["codex", "exec", "--skip-git-repo-check", "-"]
    else:
        cmd = shlex.split(backend)          # any command that reads the prompt on stdin
    out = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=timeout)
    return out.stdout


def classify_many(texts: list[str], backend: str = "claude", batch: int = 25) -> list[dict]:
    results: list[dict] = [{} for _ in texts]
    for start in range(0, len(texts), batch):
        chunk = texts[start:start + batch]
        body = "\n".join(f"[{i}] {json.dumps(redact(t[:700]), ensure_ascii=False)}" for i, t in enumerate(chunk))
        raw = _run(backend, PROMPT + body)
        m = re.search(r"\[\s*\{.*\}\s*\]", raw, re.S)
        if not m:
            continue
        try:
            for item in json.loads(m.group(0)):
                i = int(item.get("i", -1))
                if 0 <= i < len(chunk) and int(item.get("tier", 0)) in (1, 2, 3, 4):
                    results[start + i] = {"tier": int(item["tier"]), "feedback": item.get("feedback", ""), "why": item.get("why", "")}
        except (ValueError, TypeError):
            continue
    return results
