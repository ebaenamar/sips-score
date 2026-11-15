"""Close the loop: tell the agent how well it has been serving this project.

The agent never sees its own track record. `sips-score brief` turns the person's corrections in one project into a short
note the agent reads at the start of every session (SessionStart hook / AGENTS.md), and `sips-score hook prompt` warns it
in the moment when a new prompt repeats something it was already corrected on.
"""
from __future__ import annotations

import json
import re
import sys
from collections import Counter

from .analyze import Result, _content_words, _jaccard
from .redact import redact

EXPLICIT = re.compile(r"#(good|bad)\b[:\s-]*(.*)", re.I)


def explicit_ratings(res: Result) -> Counter:
    """Zero-friction explicit feedback: '#bad wrong file again' or '#good' anywhere in a normal prompt."""
    c = Counter()
    for r in res.turns:
        m = EXPLICIT.search(r.turn.text)
        if m:
            c[m.group(1).lower()] += 1
    return c


def _unique(turns, seen: set, max_items: int):
    """One line per instruction: drop exact and near repeats, and anything already listed above."""
    out = []
    for r in turns:
        w = _content_words(r.turn.text)
        if any(_jaccard(w, x) >= 0.5 for x in seen) or len(w) < 3:
            continue
        seen.add(frozenset(w))
        out.append(r)
        if len(out) == max_items:
            break
    return out


def brief(res: Result, max_items: int = 6) -> str:
    """A note written TO the agent: its track record here and what to do differently. Short on purpose."""
    rs = res.turns
    if not rs:
        return ""
    after = [r for r in rs if r.reach or r.turn.kind != "prompt"]
    rework = [r for r in after if r.rework]
    lines = [f"## How you have been doing in this project (from {len(rs)} past interventions, by sips)", ""]
    lines.append(f"- Rework: {len(rework)} of {len(after)} of your outputs were followed by a correction, retry or interruption "
                 f"({len(rework)/max(1, len(after)):.0%}).")
    rated = explicit_ratings(res)
    if rated:
        lines.append(f"- Explicit ratings: {rated['good']} #good, {rated['bad']} #bad.")
    seen: set = set()
    clusters = {cl[0].turn.id: len(cl) for cl in res.repeats}
    repeated = _unique([cl[0] for cl in res.repeats if cl[0].a.tier in (1, 2) or cl[0].a.feedback in ("criteria", "generalising")], seen, max_items)
    if repeated:
        lines += ["", "You were told these more than once. Follow them without being asked:"]
        for r in repeated:
            lines.append(f"- “{redact(r.turn.text[:160])}” (said {clusters[r.turn.id]} times)")
    named = _unique([r for r in reversed(rs) if r.a.feedback in ("pattern-report", "restated", "diagnostic")], seen, max_items)
    if named:
        lines += ["", "Recurring complaints. Check your output against them before you say you are done:"]
        for r in named:
            lines.append(f"- “{redact(r.turn.text[:160])}”")
    vague = [r for r in rs if r.a.feedback == "outcome-only" or (r.a.feedback == "pattern-report" and len(_content_words(r.turn.text)) < 3)]
    if len(vague) >= 3:
        lines += ["", f"{len(vague)} times you were only told it was still wrong. After each fix, say what you changed and why it "
                      "should now work, and ask what is still off if you are not sure."]
    loose = _unique([r for r in reversed(rs) if r.a.tier == 1 and not r.a.persisted and r.a.reasons
                     and r.a.reasons[0][0] in ("constraint", "persist-request") and len(r.turn.text.split()) <= 40], set(), max_items)
    if loose:
        lines += ["", "Rules stated in chat that are not in any rules file yet. Offer to add them to CLAUDE.md / AGENTS.md:"]
        for r in loose:
            lines.append(f"- “{redact(r.turn.text[:160])}”")
    if len(rework) / max(1, len(after)) > 0.15:
        lines += ["", "Your rework rate here is high: run the tests or check the result yourself before reporting a task as finished."]
    return "\n".join(lines)


def warn_if_repeat(res: Result, prompt: str, threshold: float = 0.5) -> str:
    """If a new prompt repeats an earlier correction in this project, say so to the agent."""
    words = _content_words(prompt)
    if len(words) < 3:
        return ""
    best, count = None, 0
    for cl in res.repeats + [[r] for r in res.turns if r.a.tier in (1, 2)]:
        if _jaccard(words, _content_words(cl[0].turn.text)) >= threshold and len(cl) >= count:
            best, count = cl[0], len(cl)
    if not best:
        return ""
    return (f"[sips] The person has told you this before in this project ({count} time{'s' if count > 1 else ''}): "
            f"“{redact(best.turn.text[:200])}”. Apply it, and if it is a lasting rule, offer to write it to CLAUDE.md / AGENTS.md.")


def _cache_path(cwd: str):
    import hashlib
    from pathlib import Path
    d = Path.home() / ".cache" / "sips-score"
    d.mkdir(parents=True, exist_ok=True)
    return d / (hashlib.sha1(cwd.encode()).hexdigest()[:16] + ".json")


def _index(res: Result) -> list[tuple[str, int]]:
    """What the prompt hook needs: past corrections and how often each was said. Redacted, kept on this machine."""
    counts = {cl[0].turn.id: len(cl) for cl in res.repeats}
    return [(redact(r.turn.text[:300]), counts.get(r.turn.id, 1)) for r in res.turns
            if r.a.tier in (1, 2) and not r.repeat_of and len(r.turn.text) <= 300]


def warn_from_index(index: list, prompt: str, threshold: float = 0.5) -> str:
    words = _content_words(prompt)
    if len(words) < 3:
        return ""
    hits = [(n, t) for t, n in index if _jaccard(words, _content_words(t)) >= threshold]
    if not hits:
        return ""
    n, text = max(hits)
    return (f"[sips] The person has told you this before in this project ({n} time{'s' if n > 1 else ''}): “{text[:200]}”. "
            "Apply it, and if it is a lasting rule, offer to write it to CLAUDE.md / AGENTS.md.")


def hook(event: str, res_for_project) -> None:
    """Claude Code hook entry point. Reads the hook JSON on stdin, prints the hook JSON answer on stdout.

    session-start does the full analysis (it may take seconds) and saves a small index; prompt only reads the index,
    so it adds no noticeable delay to each message. It never blocks: any failure means no extra context.
    """
    try:
        data = json.load(sys.stdin)
    except ValueError:
        data = {}
    cwd = data.get("cwd", "")
    text, name = "", "SessionStart" if event == "session-start" else "UserPromptSubmit"
    try:
        if event == "session-start":
            res = res_for_project(cwd)
            if res:
                text = brief(res)
                _cache_path(cwd).write_text(json.dumps(_index(res)))
        else:
            p = _cache_path(cwd)
            if p.exists():
                text = warn_from_index(json.loads(p.read_text()), data.get("prompt", ""))
    except Exception:          # a hook must never break the person's session
        text = ""
    text = text[:9000]               # Claude Code caps added context at 10,000 characters
    if text:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": name, "additionalContext": text}}))
