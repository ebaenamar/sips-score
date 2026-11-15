"""Turn classified sessions into SIPS components, a score, and findings that say what to change.

Every number keeps its inputs so the report can show how it was computed. Transcript-based proxies for the
paper's components are documented in METHOD.md; they are estimates, not the lab measurements of the paper.
"""
from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from .classify import Assignment, classify
from .model import HumanTurn, Session

REWORK_FEEDBACK = {"outcome-only", "restated", "pattern-report", "directive", "interrupt", "denial"}
SELF_REVERSAL = re.compile(r"\b(actually|scratch that|forget (what|that)|my (mistake|bad)|i was wrong|en realidad|olvida (lo|eso)|me equivoqu[eé]|error m[ií]o|mejor no)\b", re.I)
STOP = set("the a an to of in and or for on with is it this that be do i you we me my your el la los las un una de del y o en con es esto eso que no lo le se por para al".split())
FAST_APPROVAL_S = 8          # an approval this soon after the agent's last action counts as unread
SIGMA_MIN, SIGMA_BASE = 0.15, 0.5
L_REF = 100                  # leverage that maps to 1.0 on the log scale


@dataclass
class TurnResult:
    session: Session
    turn: HumanTurn
    a: Assignment
    reach: int = 0           # agent actions until the next human turn
    later_sessions: int = 0  # for persisted rules
    leverage: float = 1.0
    repeat_of: str = ""      # id of an earlier turn this one restates
    fast: bool = False
    rework: bool = False
    self_reversal: bool = False


@dataclass
class Component:
    name: str
    value: float
    formula: str
    inputs: dict


@dataclass
class Result:
    turns: list[TurnResult]
    components: dict[str, Component]
    sips: float
    by_project: dict[str, float] = field(default_factory=dict)
    by_week: list[tuple[str, float, int]] = field(default_factory=list)
    repeats: list[list[TurnResult]] = field(default_factory=list)
    findings: list[dict] = field(default_factory=list)


def _content_words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-záéíóúñ0-9_]{3,}", text.lower()) if w not in STOP}


def _jaccard(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def build(sessions: list[Session]) -> list[TurnResult]:
    out: list[TurnResult] = []
    by_project: dict[str, list[Session]] = defaultdict(list)
    for s in sessions:
        by_project[s.project].append(s)
    for s in sessions:
        prev_action_ts = None
        for t in s.turns:
            r = TurnResult(s, t, classify(t), reach=sum(1 for x in t.actions_after if x.kind == "tool") or len(t.actions_after))
            if t.ts and prev_action_ts and r.a.feedback == "acknowledge":
                r.fast = (t.ts - prev_action_ts).total_seconds() <= FAST_APPROVAL_S
            r.rework = r.a.feedback in REWORK_FEEDBACK
            r.self_reversal = bool(SELF_REVERSAL.search(t.text))
            if t.actions_after and t.actions_after[-1].ts:
                prev_action_ts = t.actions_after[-1].ts
            if (r.a.persisted or r.a.tests_written) and t.ts:
                horizon = 30 * 86400          # a rule is credited for the sessions of the next 30 days, at most 20
                r.later_sessions = min(20, sum(1 for o in by_project[s.project]
                                               if o.start and 0 < (o.start - t.ts).total_seconds() <= horizon))
            out.append(r)
    _mark_repeats(out)
    for r in out:
        r.leverage = _leverage(r, by_project)
    return out


def _mark_repeats(results: list[TurnResult]) -> None:
    """A substantive instruction that shows up again later did not stick: its leverage collapses to 1."""
    seen: dict[str, list[tuple[TurnResult, set]]] = defaultdict(list)
    for r in sorted(results, key=lambda r: (r.session.start or r.turn.ts or 0, r.turn.id) if (r.session.start or r.turn.ts) else (0, r.turn.id)):
        if r.turn.kind != "prompt" or r.a.feedback == "acknowledge":
            continue
        words = _content_words(r.turn.text)
        if len(words) < 3 or len(r.turn.text) > 600:
            continue
        for prev, pw in seen[r.session.project]:
            if prev.session is not r.session or True:
                if _jaccard(words, pw) >= 0.5:
                    r.repeat_of = prev.repeat_of or prev.turn.id
                    break
        seen[r.session.project].append((r, words))


def _leverage(r: TurnResult, by_project) -> float:
    """Downstream decisions materially changed by one human action (paper §3.1), estimated from the log."""
    if r.repeat_of:
        return 1.0
    if r.a.persisted or r.a.tests_written:          # a test re-runs in every later session, like a rule
        sessions = by_project[r.session.project]
        # each later session counts as the decisions the rule can steer there: its human turns, not every tool call
        avg_turns = sum(len(s.turns) for s in sessions) / max(1, len(sessions))
        return max(1.0, r.reach * r.a.info + r.later_sessions * avg_turns)
    # one-off: the agent's next actions only count as "changed" in proportion to the information given
    return 1.0 + max(0, r.reach - 1) * r.a.info


def score(results: list[TurnResult]) -> tuple[float, dict[str, Component]]:
    prompts = [r for r in results if r.turn.kind == "prompt"]
    n = len(results) or 1
    actions = sum(len(r.turn.actions_after) for r in results) or 1
    after_output = [r for r in results if r.reach or r.turn.kind != "prompt"]
    rework = sum(r.rework for r in after_output)
    Q = 1 - rework / max(1, len(after_output))

    mean_L = sum(r.leverage for r in results) / n
    L = sum(min(1.0, math.log10(1 + r.leverage) / math.log10(1 + L_REF)) for r in results) / n

    corrective = [r for r in results if r.a.feedback not in ("acknowledge",)]
    mean_info = sum(r.a.info for r in corrective) / max(1, len(corrective))
    rules = [r for r in results if r.a.tier == 1]
    persisted = [r for r in results if r.a.persisted or r.a.tests_written]
    persist_rate = (len(persisted) + 0.5) / (len(rules) + 1)          # smoothed so a few sessions don't read as zero
    lam = min(1.0, 0.5 * mean_info + 0.5 * min(1.0, persist_rate))

    interrupts = sum(r.turn.kind in ("interrupt", "denial") for r in results)
    per_action = (n + interrupts) / actions
    gamma = 100 * min(1.0, per_action / 0.5)

    reasoning = sum(1 for r in prompts if r.a.technical or r.a.feedback in ("diagnostic", "question", "example", "criteria", "generalising"))
    acks = [r for r in results if r.a.feedback == "acknowledge"]
    fast = sum(r.fast for r in acks)
    sigma = 0.6 * reasoning / max(1, len(prompts)) + 0.4 * (1 - (len(acks) + fast) / (2 * n))
    S = 1.0 if sigma >= SIGMA_BASE else (0.0 if sigma < SIGMA_MIN else (sigma - SIGMA_MIN) / (SIGMA_BASE - SIGMA_MIN))

    eps = sum(r.self_reversal for r in prompts) / max(1, len(prompts))

    factors = [Q, L, lam, 1 - eps, S, 1 / (1 + gamma / 100)]
    raw = Q * L * lam * (1 - eps) * S / (1 + gamma / 100)            # the paper's formula, eq. (2)
    # Readable index: 100 × geometric mean of the six factors. Any factor at 0 still collapses it to 0.
    sips = 100 * math.prod(max(0.0, f) for f in factors) ** (1 / len(factors))
    comps = {
        "Q": Component("Quality", Q, "1 − rework turns / turns after an agent output",
                       {"rework turns": rework, "turns after output": len(after_output)}),
        "L": Component("Leverage", L, f"mean over turns of log10(1 + leverage) / log10(1 + {L_REF})",
                       {"mean leverage": round(mean_L, 2), "turns": n}),
        "λ": Component("Learning signal", lam, "0.5 × mean feedback information + 0.5 × share of rules made persistent",
                       {"mean information": round(mean_info, 2), "rules stated": len(rules), "made persistent (file or test)": len(persisted)}),
        "γ": Component("Cognitive load (proxy, 0–100)", gamma, "100 × min(1, (interventions + interrupts) / agent actions / 0.5)",
                       {"interventions": n, "interrupts or denials": interrupts, "agent actions": actions}),
        "σ": Component("Skill preservation (proxy)", sigma, "0.6 × share of prompts with reasoning or code + 0.4 × (1 − approvals and fast approvals / 2n)",
                       {"prompts with reasoning": reasoning, "prompts": len(prompts), "approvals": len(acks), f"approvals < {FAST_APPROVAL_S}s": fast,
                        "S(σ)": round(S, 2)}),
        "ε": Component("Human error (self-reversals)", eps, "prompts where you reverse your own earlier instruction / prompts",
                       {"self-reversals": sum(r.self_reversal for r in prompts)}),
    }
    comps["raw"] = Component("Paper formula (eq. 2)", raw, "Q × L × λ × (1 − ε) × S(σ) / (1 + γ/100)", {})
    return sips, comps


def analyze(sessions: list[Session]) -> Result:
    results = build(sessions)
    sips, comps = score(results)
    res = Result(results, comps, sips)
    groups: dict[str, list[TurnResult]] = defaultdict(list)
    for r in results:
        groups[r.session.project].append(r)
    res.by_project = {p: score(rs)[0] for p, rs in groups.items() if len(rs) >= 10}
    weeks: dict[str, list[TurnResult]] = defaultdict(list)
    for r in results:
        if r.turn.ts:
            weeks[r.turn.ts.strftime("%G-W%V")].append(r)
    res.by_week = [(w, score(rs)[0], len(rs)) for w, rs in sorted(weeks.items()) if len(rs) >= 10]
    clusters: dict[str, list[TurnResult]] = defaultdict(list)
    for r in results:
        if r.repeat_of:
            clusters[r.repeat_of].append(r)
    first = {r.turn.id: r for r in results}
    res.repeats = sorted(([first[k]] + v for k, v in clusters.items() if k in first), key=len, reverse=True)
    res.findings = findings(res)
    return res


def _what_if(results: list[TurnResult], change) -> float:
    import copy
    rs = copy.copy(results)
    rs = change([copy.copy(r) for r in rs])
    return score(rs)[0]


def findings(res: Result) -> list[dict]:
    out = []
    rs = res.turns
    c = res.components
    # 1. repeated instructions -> persist them
    if res.repeats:
        top = res.repeats[:5]
        ids = {r.turn.id for cl in top for r in cl[1:]}
        def persist(turns):
            keep = [t for t in turns if t.turn.id not in ids]
            for t in keep:
                if any(t.turn.id == cl[0].turn.id for cl in top):
                    t.a = copy_assign(t.a, persisted=["CLAUDE.md"]); t.leverage = max(t.leverage, 50)
            return keep
        new = _what_if(rs, persist)
        procedural = sum(1 for cl in top if len(cl[0].turn.text.split()) <= 12 and cl[0].a.tier == 4)
        out.append({"title": "Turn repeated instructions into rules or commands",
                    "evidence": [f"“{cl[0].turn.text[:90]}” — said {len(cl)} times" for cl in top],
                    "why": "Each repetition is a tier-4 action with leverage 1. "
                           + ("Short procedures you repeat (" + str(procedural) + " of these) belong in a custom command or skill (e.g. .claude/commands/ship.md); " if procedural else "")
                           + "constraints you repeat belong in CLAUDE.md / AGENTS.md or a hook, where they apply to every later session.",
                    "impact": (res.sips, new)})
    # 2. outcome-only feedback -> say why
    oo = [r for r in rs if r.a.feedback in ("outcome-only", "restated")]
    if len(oo) >= max(3, 0.08 * len(rs)):
        def explain(turns):
            for t in turns:
                if t.a.feedback in ("outcome-only", "restated"):
                    t.a = copy_assign(t.a, info=0.85, feedback="diagnostic")
                    t.rework = False
            return turns
        out.append({"title": "Say why, not only that it failed",
                    "evidence": [f"“{r.turn.text[:90]}”" for r in oo[:4]],
                    "why": f"{len(oo)} turns ({len(oo)/len(rs):.0%}) only say the output was wrong. The agent can fix that case, but nothing carries to the next one.",
                    "impact": (res.sips, _what_if(rs, explain))})
    # 3. fast approvals -> skill risk
    acks = [r for r in rs if r.a.feedback == "acknowledge"]
    fast = [r for r in acks if r.fast]
    if len(acks) >= 0.25 * len(rs) or len(fast) >= 5:
        out.append({"title": "Fewer approvals, more checks that run themselves",
                    "evidence": [f"{len(acks)} approvals ({len(acks)/len(rs):.0%} of turns), {len(fast)} within {FAST_APPROVAL_S}s of the agent's last action"],
                    "why": "Approving many small steps is tier-4 review: the paper's most automation-bias-prone point. Pre-approve safe tools, and ask for tests or a self-check instead.",
                    "impact": None})
    # 4. rules that never persisted
    loose = [r for r in rs if r.a.tier == 1 and not r.a.persisted and len(r.turn.text.split()) <= 40
             and r.a.reasons and r.a.reasons[0][0] in ("constraint", "persist-request")]
    if loose and c["λ"].inputs["made persistent (file or test)"] < len(loose):
        out.append({"title": "Rules said in chat are lost at the end of the session",
                    "evidence": [f"“{r.turn.text[:90]}”" for r in loose[:4]],
                    "why": f"{len(loose)} constraints were stated in chat but not written to a rules file. You speak at tier 1 but get tier-4 leverage.",
                    "impact": None})
    # 5. frustration
    fr = [r for r in rs if r.a.frustrated]
    if len(fr) >= 2:
        out.append({"title": "Frustration marks where the setup is failing",
                    "evidence": [f"“{r.turn.text[:90]}”" for r in fr[:3]],
                    "why": "Capitals and 'I already said' usually follow an instruction that did not persist. Those are the first rules to write down.",
                    "impact": None})
    for f in out:                                   # same text pasted in two sessions should show once
        uniq = []
        for e in f["evidence"]:
            if e not in uniq:
                uniq.append(e)
        f["evidence"] = uniq
        if f["impact"]:
            f["gain"] = f["impact"][1] - f["impact"][0]
    return sorted(out, key=lambda f: -f.get("gain", 0))


def copy_assign(a: Assignment, **kw) -> Assignment:
    import dataclasses
    return dataclasses.replace(a, **kw)
