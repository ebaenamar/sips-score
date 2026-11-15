"""sips — estimate your Sustainable Intervention Point Score from your own AI-agent sessions. Runs locally."""
from __future__ import annotations

import argparse
import json
import random
import sys
from collections import Counter
from pathlib import Path

from . import __version__
from .adapters import load
from .analyze import Result, analyze
from .classify import classify
from .model import HumanTurn
from .redact import redact

TIER = {1: "Strategic control", 2: "Calibration", 3: "Informed oversight", 4: "Mechanical review"}


def bar(v: float, width: int = 20) -> str:
    v = max(0.0, min(1.0, v))
    return "█" * round(v * width) + "·" * (width - round(v * width))


def report(res: Result, top: int = 3) -> str:
    rs = res.turns
    L = []
    w = L.append
    sessions = {r.session.id for r in rs}
    w(f"SIPS {res.sips:5.1f} / 100   ({len(rs)} human interventions, {len(sessions)} sessions)")
    w(f"     index = 100 × geometric mean of the six factors · paper formula (eq. 2) = {res.components['raw'].value:.3f}\n")
    w("Components (each is a transcript-based estimate; see METHOD.md)")
    for key, c in res.components.items():
        if key == "raw":
            continue
        shown = c.value / 100 if key == "γ" else c.value
        w(f"  {key} {c.name:<32} {bar(shown)} {c.value:6.2f}")
        w(f"      = {c.formula}")
        w("        " + ", ".join(f"{k}: {v}" for k, v in c.inputs.items()))
    w("")
    tiers = Counter(r.a.tier for r in rs)
    w("Where your interventions go")
    for t in (1, 2, 3, 4):
        lev = [r.leverage for r in rs if r.a.tier == t]
        med = sorted(lev)[len(lev) // 2] if lev else 0
        w(f"  Tier {t} {TIER[t]:<20} {bar(tiers[t] / max(1, len(rs)))} {tiers[t]:5d} ({tiers[t]/max(1,len(rs)):4.0%})  median leverage {med:6.1f}")
    fb = Counter(r.a.feedback for r in rs)
    w("\nHow you give feedback (information the agent can reuse, 0–1)")
    infos = {r.a.feedback: r.a.info for r in rs}
    for k, v in fb.most_common(8):
        w(f"  {k:<15} {v:5d}  info {infos[k]:.2f}")
    if res.by_week:
        w("\nTrend by week")
        for wk, s, n in res.by_week[-8:]:
            w(f"  {wk}  {bar(s / 100, 30)} {s:5.1f}  ({n} turns)")
    if len(res.by_project) > 1:
        w("\nBy project (≥10 interventions)")
        for p, s in sorted(res.by_project.items(), key=lambda x: -x[1])[:8]:
            w(f"  {s:5.1f}  {p[-60:]}")
    w("\nWhat would raise your score")
    if not res.findings:
        w("  Nothing stands out. Keep stating rules once and persisting them.")
    for i, f in enumerate(res.findings[:top], 1):
        imp = f" (estimated SIPS {f['impact'][0]:.1f} → {f['impact'][1]:.1f})" if f.get("impact") else ""
        w(f"  {i}. {f['title']}{imp}")
        w(f"     Why: {f['why']}")
        for e in f["evidence"][:3]:
            w(f"       · {redact(e)}")
    w("\nRun `sips-score explain <turn-id>` on any turn to see why it got its tier. Turn ids: `sips-score analyze --list`.")
    return "\n".join(L)


def explain_turn(r) -> str:
    a = r.a
    out = [f"{r.turn.id}  [{r.session.harness} · {r.session.project[-40:]}]",
           f"  text: “{redact(r.turn.text[:300])}”",
           f"  tier {a.tier} ({TIER[a.tier]}), confidence {a.confidence:.2f}"]
    for rid, m, why in a.reasons:
        out.append(f"    rule {rid}: matched “{m}” → {why}")
    out.append(f"  feedback: {a.feedback} (information {a.info:.2f}) — matched “{a.feedback_why[0]}”: {a.feedback_why[1]}")
    out.append(f"  agent actions until your next turn: {r.reach}")
    if a.tests_written:
        out.append(f"  test written: {', '.join(sorted(set(a.tests_written)))} → re-runs in {r.later_sessions} later sessions")
    if a.persisted:
        out.append(f"  persisted to: {', '.join(sorted(set(a.persisted)))} → applies to {r.later_sessions} later sessions")
    if r.repeat_of:
        out.append(f"  repeats {r.repeat_of}: the earlier instruction did not stick, so leverage = 1")
    out.append(f"  estimated leverage: {r.leverage:.1f}" + ("" if a.persisted or a.tests_written or r.repeat_of else f"  (= 1 + (reach − 1) × information)"))
    if a.frustrated:
        out.append("  frustration markers present")
    return "\n".join(out)


def main(argv=None):
    import signal
    if hasattr(signal, "SIGPIPE"):
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)   # `sips-score analyze --list | head` should just stop
    p = argparse.ArgumentParser(prog="sips-score", description=__doc__)
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd", required=True)
    rp = sub.add_parser("repo", help="score the human-AI design of a project from its repo (static, nothing is run)")
    rp.add_argument("path", nargs="?", default=".")
    rp.add_argument("--json", help="also write the result as JSON")
    rp.add_argument("--no-sessions", action="store_true", help="do not look for this project's Claude Code sessions")
    ap = sub.add_parser("askf1", help="HiL-Bench Ask-F1 from scenario results (JSONL: blocker, asked, question_ok)")
    ap.add_argument("file")
    hp = sub.add_parser("hook", help="Claude Code hook: reads hook JSON on stdin")
    hp.add_argument("event", choices=["session-start", "prompt"])
    for name in ("analyze", "explain", "label", "eval", "brief"):
        sp = sub.add_parser(name)
        sp.add_argument("--harness", default="auto", choices=["auto", "claude-code", "codex", "generic"])
        sp.add_argument("--path", help="log file or folder (defaults: ~/.claude/projects, ~/.codex/sessions)")
        sp.add_argument("--project", help="only sessions whose project contains this text")
        sp.add_argument("--since", help="only sessions starting on or after YYYY-MM-DD (compare before/after a change)")
        sp.add_argument("--until", help="only sessions starting before YYYY-MM-DD")
        if name == "analyze":
            sp.add_argument("--json", help="also write the full result as JSON to this file")
            sp.add_argument("--list", action="store_true", help="list turns with id, tier and feedback type")
            sp.add_argument("--top", type=int, default=3)
        if name == "explain":
            sp.add_argument("turn_id")
        if name == "brief":
            sp.add_argument("--write", help="write the note into this file between <!-- sips --> markers (e.g. AGENTS.md for Codex)")
        if name in ("label", "eval"):
            sp.add_argument("--file", default="sips-labels.jsonl")
        if name in ("eval",):
            sp.add_argument("--llm", help="also score an LLM classifier: claude | codex | any command reading stdin")
        if name == "label":
            sp.add_argument("-n", type=int, default=40)
    args = p.parse_args(argv)

    if args.cmd == "repo":
        from .repo import scan, report as repo_report
        res = scan(args.path)
        if not args.no_sessions:
            pr = project_result(res.root)
            if pr:
                res.sessions_sips = pr.sips
        print(repo_report(res))
        if args.json:
            Path(args.json).write_text(json.dumps({
                "sips": res.sips, "components": {k: {"name": n, "value": v, "why": w} for k, (n, v, w) in res.components.items()},
                "hits": [h.__dict__ for h in res.hits], "findings": res.findings, "commits": res.commits, "ai_commits": res.ai_commits,
                "sessions_sips": res.sessions_sips}, indent=1, default=str))
        return
    if args.cmd == "askf1":
        from .repo import askf1
        r = askf1([json.loads(l) for l in open(args.file) if l.strip()])
        print(f"Ask-F1 {r['ask_f1']:.2f}   precision {r['precision']:.2f} · recall {r['recall']:.2f} · {r['runs']} runs")
        for k, v in r["breakdown"].items():
            print(f"  {k:<38} {v}")
        return
    if args.cmd == "hook":
        from .brief import hook
        return hook(args.event, project_result)

    sessions = load(args.harness, args.path)
    if args.project:
        sessions = [s for s in sessions if args.project in s.project]
    for bound, keep in ((args.since, lambda d, b: d >= b), (args.until, lambda d, b: d < b)):
        if bound:
            sessions = [s for s in sessions if s.start and keep(s.start.date().isoformat(), bound)]
    if not sessions:
        sys.exit("No sessions found. Point --path at your logs, or use --harness generic with a JSON/JSONL chat file.")

    if args.cmd == "label":
        return label(sessions, args.file, args.n)
    if args.cmd == "eval":
        return evaluate(args.file, getattr(args, "llm", None))

    res = analyze(sessions)
    if args.cmd == "brief":
        from .brief import brief
        note = brief(res) or "No interventions in this project yet."
        if args.write:
            write_block(args.write, note)
            print(f"Updated {args.write}")
        else:
            print(note)
        return
    if args.cmd == "explain":
        match = [r for r in res.turns if r.turn.id == args.turn_id]
        print(explain_turn(match[0]) if match else f"No turn {args.turn_id}")
        return
    if args.list:
        for r in res.turns:
            print(f"{r.turn.id:<16} T{r.a.tier} {r.a.feedback:<15} L={r.leverage:6.1f}  {redact(r.turn.text[:70])!r}")
        return
    print(report(res, args.top))
    if args.json:
        Path(args.json).write_text(json.dumps({
            "sips": res.sips,
            "components": {k: {"value": c.value, "formula": c.formula, "inputs": c.inputs} for k, c in res.components.items()},
            "turns": [{"id": r.turn.id, "harness": r.session.harness, "project": r.session.project, "tier": r.a.tier,
                       "confidence": r.a.confidence, "feedback": r.a.feedback, "info": r.a.info, "leverage": r.leverage,
                       "reasons": [(i, redact(m), w) for i, m, w in r.a.reasons], "repeat_of": r.repeat_of} for r in res.turns],
            "findings": [{**f, "evidence": [redact(e) for e in f["evidence"]]} for f in res.findings],
        }, indent=1, default=str))


def write_block(path: str, note: str):
    """Replace the sips block in a rules file, leaving everything the person wrote untouched."""
    import re as _re
    start, end = "<!-- sips:start -->", "<!-- sips:end -->"
    block = f"{start}\n{note}\n{end}"
    p = Path(path)
    text = p.read_text() if p.exists() else ""
    if start in text and end in text:
        text = _re.sub(_re.escape(start) + ".*?" + _re.escape(end), lambda m: block, text, flags=_re.S)
    else:
        text = (text.rstrip() + "\n\n" if text.strip() else "") + block + "\n"
    p.write_text(text)


def project_result(cwd: str):
    """The analysis for the project the agent is working in. Claude Code names project folders after the path."""
    import os
    import re as _re
    from .adapters import claude_code
    if not cwd:
        return None
    folder = os.path.expanduser("~/.claude/projects/" + _re.sub(r"[^A-Za-z0-9]", "-", cwd))
    sessions = list(claude_code(folder)) if os.path.isdir(folder) else []
    return analyze(sessions) if sessions else None


def label(sessions, file: str, n: int):
    """Hand-label a random sample of your turns so the classifier can be measured (and improved)."""
    turns = [t for s in sessions for t in s.turns if t.kind == "prompt"]
    random.seed(7)
    sample = random.sample(turns, min(n, len(turns)))
    done = {json.loads(l)["id"] for l in open(file)} if Path(file).exists() else set()
    print("Label each turn: 1 strategic · 2 calibration · 3 oversight · 4 mechanical · s skip · q quit\n")
    with open(file, "a") as f:
        for t in sample:
            if t.id in done:
                continue
            print(f"\n“{redact(t.text[:500])}”")
            ans = input("tier> ").strip().lower()
            if ans == "q":
                break
            if ans in ("1", "2", "3", "4"):
                f.write(json.dumps({"id": t.id, "text": redact(t.text), "tier": int(ans)}) + "\n")


def evaluate(file: str, llm: str | None = None):
    """Compare the classifier with human labels: accuracy, per-tier precision and recall, confusion matrix."""
    rows = [json.loads(l) for l in open(file)]
    print("— rules —")
    _report_eval(rows, [classify(HumanTurn(r["id"], r["text"])).tier for r in rows])
    if llm:
        from .llm import classify_many
        out = classify_many([r["text"] for r in rows], llm)
        pred = [o.get("tier", classify(HumanTurn(r["id"], r["text"])).tier) for o, r in zip(out, rows)]
        print(f"\n— LLM ({llm}), {sum(1 for o in out if o)} of {len(rows)} answered —")
        _report_eval(rows, pred)


def _report_eval(rows, pred):
    gold = [r["tier"] for r in rows]
    acc = sum(p == g for p, g in zip(pred, gold)) / len(rows)
    print(f"{len(rows)} labelled turns · accuracy {acc:.0%}")
    print("tier  precision  recall  n")
    for t in (1, 2, 3, 4):
        tp = sum(p == g == t for p, g in zip(pred, gold))
        pp, gg = pred.count(t), gold.count(t)
        print(f"  {t}    {tp/pp if pp else 0:6.0%}   {tp/gg if gg else 0:6.0%}  {gg}")
    print("confusion (rows = human, cols = sips)")
    for g in (1, 2, 3, 4):
        print(f"  {g}  " + " ".join(f"{sum(1 for p, h in zip(pred, gold) if h == g and p == c):4d}" for c in (1, 2, 3, 4)))
    off = [(r, p) for r, p in zip(rows, pred) if p != r["tier"]][:10]
    if off:
        print("\nDisagreements to fix rules with:")
        for r, p in off:
            print(f"  human {r['tier']} / sips {p}: “{r['text'][:100]}”")


if __name__ == "__main__":
    main()
