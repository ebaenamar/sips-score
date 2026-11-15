"""Score the human-AI design of a project from its repository. Static only: nothing in the repo is executed.

Two questions, answered with file:line evidence:
  1. Where does the solution put the human?  (rules, calibration, oversight, per-step approval, escalation)
  2. How was it built with AI?               (git history, AI co-authored commits, rules files, tests, agent sessions)

Escalation follows HiL-Bench (Trinh et al., 2026, arXiv:2604.09408): a good loop asks a person when it hits a blocker
it cannot resolve, and does not ask otherwise. Run `sips-score askf1` on scenario results to measure it.
"""
from __future__ import annotations

import math
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "env", "dist", "build", "__pycache__", ".next", "out", "target", ".mypy_cache", ".pytest_cache"}
CODE_EXT = {".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs", ".java", ".rb", ".md", ".yaml", ".yml", ".json", ".toml", ".txt", ".prompt", ".jinja"}
MAX_FILE = 400_000

RULE_FILES = re.compile(r"^((\.claude|\.github|\.codex)/)?(CLAUDE\.md|AGENTS\.md|GEMINI\.md|\.cursorrules|copilot-instructions\.md)$|^\.cursor/rules/.+$")
DOCS_DIR = re.compile(r"(^|/)(docs?|documentation|site-docs|vendor|third_party|examples?)/", re.I)
TEST_PATH = re.compile(r"(^|/)(tests?|spec|__tests__|evals?)/|(_test|\.test|\.spec|test_|eval_)[^/]*\.\w+$", re.I)

# (signal, tier or role, regex, what it means)
PATTERNS = [
    ("guardrail", 1, re.compile(r"\b(guardrails?|allow_?list|deny_?list|forbidden_?(actions|tools|words)|max_(spend|budget|amount|cost|refund|price)|budget_limit|spend_limit|(confidence|risk|score|approval)_?threshold)\b", re.I),
     "a rule the loop must respect: set once, applies to every run"),
    ("system-prompt-rule", 1, re.compile(r"\b(never|must not|always|do not)\b.{0,80}", re.I),
     "a standing instruction in a prompt or rules file"),
    ("eval-set", 2, re.compile(r"(\bexpected\s*=|[\"']expected[\"']\s*:|\bground[_ ]truth\b|\bgolden\b|\bgold_label\b|\beval(uation)?_?(set|cases|dataset)\b|\bfew[_ -]?shot\b|\bexamples?\s*=\s*[\[(])", re.I),
     "examples or expected outputs the loop is checked against"),
    ("feedback-capture", 2, re.compile(r"\b(feedback\s*[=:\[(]|(save|store|log|record|collect|capture)_?(human_?|user_?)?feedback|human_?feedback|feedback_?(log|store|db|table|history)|rejection_?reasons?|thumbs_?(up|down)|user_?rating|human_?label|user_?corrections?|(\w*_)?input\(\s*[frbFRB]?[\"'][^\"']{0,80}(suggest|feedback|correct|edit|revis|change|improve|comment)|handle_?edit\b|set_?edited\w*|onEdit\b|edited_?(text|description|draft|output|content|response)\b)", re.I),
     "captures what a person corrected or rated"),
    ("oversight", 3, re.compile(r"\b(log_?(decision|event|outcome)s?|metrics?\.(inc|record|log|push|observe)|audit_?log|review_?queue|sample_?for_?review|langfuse|langsmith|wandb|opentelemetry|mlflow|@trace|trace\()", re.I),
     "lets a person see and review the loop's decisions as a set"),
    ("per-step-approval", 4, re.compile(r"(input\(\s*[frbFRB]?[\"'][^\"']*(approve|confirm|continue|\by/n\b|\[y\b|proceed)|\bhuman_?approval\b|\bawait_?approval\b|\brequire_?approval\b|\bconfirm\(|\bapprove_each\b|\b(wait_for|request|send_for|ask_for)_?approval\b|\bapproval_status\b|\bis_approved\b|\bif\s+approved\b|\bapprove_(post|step|action|draft|message|reply)\b|\bst\.button\(\s*[frbFRB]?[\"'][^\"']*(approve|confirm|accept|publish|proceed|continue|process|go ahead|looks good)|\btext:\s*[\"'](accept|approve|reject|confirm)[\"']|\bhandle_?(accept|approve|reject)\b|\bon_?(accept|approve|reject)\b|\bhuman_input_mode\s*=\s*[\"']ALWAYS|[\"'](yes|y|ok)[\"']\s+in\s+\w+(\.lower\(\))?)", re.I),
     "asks a person to approve each step: tier-4, prone to rubber-stamping"),
    ("escalation", "ask", re.compile(r"\b(escalat\w*_?(to_?)?(human|user|operator|manager|person|support)|escalate\(|ask_?(the_?)?(human|user|operator)|(handoff|hand_off)_?(to_?)?(human|user|operator|person)|human_?handoff|request_?(human_?)?clarification|ask_?for_?clarification|human[-_ ]?in[-_ ]?the[-_ ]?loop|hitl|human_?review|needs_?human|interrupt\()|confidence\s*[<>]=?\s*0?\.\d", re.I),
     "hands a case to a person (or asks) under some condition"),
    ("learning-loop", "learn", re.compile(r"\b(self[_-]?improv\w*|reflect(ion)?|update_?(prompt|policy|memory|rules)|optimi[sz]e_?prompt|dspy|retrain|fine[_-]?tune|memory\.(add|save|write|update)|lessons?_?learned)\b", re.I),
     "feeds results or corrections back so the next run is better"),
]
AI_USE = re.compile(r"\b(openai|anthropic|langchain|langgraph|crewai|autogen|llama_?index|ollama|google\.generativeai|google\.genai|generativeai|@ai-sdk|replicate|groq|mistralai|cohere|litellm|dspy|transformers|huggingface_hub|gpt-[345]|gpt-4o|claude-[0-9a-z]|gemini|vertexai|bedrock|deepseek|perplexity|fastmcp|modelcontextprotocol|mcp\.server|chat\.completions|generate_content|\bllm\.(invoke|generate|chat|complete))", re.I)
GUESS_RULE = re.compile(r"(come up with|make up|invent|fabricate|guess|assume)\b.{0,60}\b(data|values?|answers?|fields?|info\w*|details?)\b|creative ways to fill|if (you are |you're )?(unsure|not sure|uncertain)\b.{0,40}\b(guess|assume|best)", re.I)
AUTONOMY_FLAG = re.compile(r"\bhuman_input_mode\s*=\s*[\"']NEVER[\"']")
AI_TRAILER = re.compile(r"co-authored-by:\s*.*(claude|anthropic|codex|openai|copilot|cursor|gpt|gemini|devin|aider)", re.I)


@dataclass
class Hit:
    signal: str
    role: object
    path: str
    line: int
    text: str
    why: str


@dataclass
class RepoResult:
    root: str
    files: int = 0
    hits: list[Hit] = field(default_factory=list)
    rule_files: list[str] = field(default_factory=list)
    test_files: list[str] = field(default_factory=list)
    commits: int = 0
    ai_commits: int = 0
    rule_commits: int = 0
    components: dict = field(default_factory=dict)
    sips: float = 0.0
    findings: list[dict] = field(default_factory=list)
    sessions_sips: float | None = None
    uses_ai: bool = False
    verdict: str = ""
    autonomy_flags: list[str] = field(default_factory=list)
    guess_rules: list[str] = field(default_factory=list)


_CODE_CHARS = re.compile(r"[=(){}\[\];]|\w\.\w|->|=>|^\s*(def|class|if|for|return|const|let|function|import)\b")


def _prose(line: str) -> bool:
    """Sentences in UI copy or docs mention 'metrics' or 'monitoring' without building any. Count code, not prose."""
    return len(line.split()) >= 8 and not _CODE_CHARS.search(line)


def _in_string(line: str, pos: int) -> bool:
    """True if pos falls inside a quoted string literal (UI labels, docs in code)."""
    before = line[:pos]
    return any(before.count(q) % 2 == 1 for q in ('"', "'", "`"))


LOCKFILE = re.compile(r"(lock\.json|\.lock|lockfile|yarn\.lock|pnpm-lock\.yaml)$", re.I)


def _walk(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".") or d in (".claude", ".cursor", ".github")]
        for fn in filenames:
            p = Path(dirpath) / fn
            if LOCKFILE.search(fn):
                continue
            if p.suffix.lower() in CODE_EXT or RULE_FILES.search(str(p.relative_to(root))):
                yield p


def _git(root: Path, *args) -> str:
    try:
        return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, timeout=60).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def scan(root_str: str) -> RepoResult:
    root = Path(root_str).expanduser().resolve()
    res = RepoResult(str(root))
    seen_per_signal: dict[str, int] = {}
    for p in _walk(root):
        rel = str(p.relative_to(root))
        try:
            if p.stat().st_size > MAX_FILE:
                continue
            text = p.read_text(errors="ignore")
        except OSError:
            continue
        res.files += 1
        if not res.uses_ai and p.suffix.lower() not in (".md", ".txt") and AI_USE.search(text):
            res.uses_ai = True
        for n, line in enumerate(text.splitlines(), 1):
            if p.suffix.lower() not in (".md", ".txt") and len(line) < 600 and GUESS_RULE.search(line) and len(res.guess_rules) < 5:
                res.guess_rules.append(f"{rel}:{n}  {line.strip()[:120]}")
            if AUTONOMY_FLAG.search(line) and not line.lstrip().startswith(("#", "//")):
                res.autonomy_flags.append(f"{rel}:{n}  {line.strip()[:100]}")
        if RULE_FILES.search(rel):
            res.rule_files.append(rel)
        if TEST_PATH.search(rel):
            res.test_files.append(rel)
        if DOCS_DIR.search(rel) and not RULE_FILES.search(rel):
            continue                     # documentation and vendored examples describe other projects
        is_prompt = bool(RULE_FILES.search(rel) or re.search(r"prompt|system|instruction", rel, re.I) or p.suffix in (".prompt", ".jinja"))
        if p.suffix.lower() in (".md", ".txt") and not is_prompt:
            continue                     # READMEs and docs describe intentions; rules files and prompts are read
        for n, line in enumerate(text.splitlines(), 1):
            if len(line) > 400 or (not is_prompt and _prose(line)):
                continue
            for sig, role, rx, why in PATTERNS:
                if sig == "system-prompt-rule" and not is_prompt:
                    continue
                if seen_per_signal.get(sig, 0) >= 40:      # enough evidence; keeps reports readable
                    continue
                m = rx.search(line)
                if m and (is_prompt or not _in_string(line, m.start())):
                    res.hits.append(Hit(sig, role, rel, n, line.strip()[:140], why))
                    seen_per_signal[sig] = seen_per_signal.get(sig, 0) + 1
    log = _git(root, "log", "--format=%H%x1f%B%x1e")
    commits = [c for c in log.split("\x1e") if c.strip()]
    res.commits = len(commits)
    res.ai_commits = sum(bool(AI_TRAILER.search(c)) for c in commits)
    if res.rule_files:
        res.rule_commits = len([l for l in _git(root, "log", "--format=%H", "--", *res.rule_files).splitlines() if l.strip()])
    _score(res)
    return res


def _count(res, *sigs):
    """Distinct touchpoints: the same signal repeated in one file is one place where a person steps in."""
    return len({(h.signal, h.path) for h in res.hits if h.signal in sigs})


def _score(res: RepoResult) -> None:
    """Same six factors as eq. 2 of the paper, estimated from the design. Each one keeps its inputs."""
    t1 = _count(res, "guardrail", "system-prompt-rule") + 3 * len(res.rule_files)
    t2 = _count(res, "eval-set", "feedback-capture") + len(res.test_files)
    t3 = _count(res, "oversight")
    t4 = _count(res, "per-step-approval")
    esc = _count(res, "escalation")
    learn = _count(res, "learning-loop")
    touch = t1 + t2 + t3 + t4 or 1
    # leverage: where the human touchpoints sit, weighted by the paper's leverage tiers (log scale, ~1 to ~1000)
    tier_lev = {1: 1000, 2: 300, 3: 30, 4: 1}
    mean_log = (t1 * math.log10(1 + tier_lev[1]) + t2 * math.log10(1 + tier_lev[2]) + t3 * math.log10(1 + tier_lev[3]) + t4 * math.log10(2)) / touch
    n_touch = t1 + t2 + t3 + t4
    # shrink toward low when there are only one or two signals: one test file is not a human-in-the-loop design
    L = max(0.1, min(1.0, mean_log / math.log10(1 + 1000)) * n_touch / (n_touch + 3)) if n_touch else 0.1
    Q = min(1.0, 0.3 + 0.15 * min(len(res.test_files), 3) + 0.1 * min(_count(res, "eval-set"), 2))
    lam = min(1.0, 0.1 + 0.45 * bool(_count(res, "feedback-capture")) + 0.45 * bool(learn))
    # load grows with each place a person must click approve; a single gate is light, approving every item is not
    gamma = min(100.0, 20 * t4 + (20 if not esc else 0))
    sigma = min(1.0, 0.25 + 0.25 * bool(t1) + 0.25 * bool(t2) + 0.25 * bool(t3))
    if not (t1 + t3 + t4 + esc + learn + _count(res, "feedback-capture")):
        L, sigma = 0.1, 0.25          # tests alone do not put a person in the loop
    S = 1.0 if sigma >= 0.5 else (0.0 if sigma < 0.15 else (sigma - 0.15) / 0.35)
    eps = 0.0 if res.rule_commits >= 1 or not res.rule_files else 0.1   # rules under version control can be reviewed and rolled back
    ask = 1.0 if esc else 0.6                                         # no escalation path: every blocker becomes a silent guess
    factors = [Q, L, lam, 1 - eps, S, 1 / (1 + gamma / 100), ask]
    res.sips = 100 * math.prod(max(0.0, f) for f in factors) ** (1 / len(factors))
    res.components = {
        "Q": ("Quality checks", Q, f"{len(res.test_files)} test/eval files, {_count(res, 'eval-set')} expected-output markers"),
        "L": ("Leverage of human touchpoints", L, f"rules {t1} · calibration {t2} · oversight {t3} · per-step approvals {t4}"),
        "λ": ("Learning signal", lam, f"feedback captured: {'yes' if _count(res, 'feedback-capture') else 'no'} · fed back into the loop: {'yes' if learn else 'no'}"),
        "γ": ("Human load (0–100)", gamma, f"{t4} places that need a click to approve{'' if esc else ' · no escalation path (+20)'}"),
        "σ": ("Skill preservation", sigma, "people write rules / examples / review sets, not only approve"),
        "ε": ("Human error risk", eps, f"rules files: {len(res.rule_files)} · commits touching them: {res.rule_commits}"),
        "ask": ("Escalation (HiL-Bench)", ask, f"{esc} escalation points found" + ("" if esc else " · measure it with `sips-score askf1`")),
    }
    res.findings = _findings(res, t1, t2, t3, t4, esc, learn)
    human = t1 + t3 + t4 + esc + learn + _count(res, "feedback-capture")
    if not res.uses_ai:
        res.verdict = "No AI calls found: SIPS does not apply to this project."
    elif not human:
        res.verdict = ("Fully autonomous: the AI acts and no person steps in anywhere in the code"
                       + (" (only tests or examples were found)." if t2 else "."))
        res.findings = [{"title": "Decide where a person must step in",
                         "why": "Nothing in the code hands a case to a person, sets a limit, or learns from a correction. Start with the two "
                                "highest-leverage moves: one rule the loop can never break (e.g. a spend or action limit), and an escalation "
                                "to a person when input is missing or confidence is low.",
                         "evidence": res.autonomy_flags[:3] or ["AI calls found, no human touchpoints in code"]}] + [f for f in res.findings if "escalation" not in f["title"].lower()][:2]


def _ev(res, sig, k=2):
    return [f"{h.path}:{h.line}  {h.text}" for h in res.hits if h.signal == sig][:k]


def _findings(res, t1, t2, t3, t4, esc, learn):
    out = []
    if res.guess_rules:
        out.append({"title": "Stop telling the agent to guess: ask instead",
                    "why": "A prompt here tells the agent to make up what it does not know. That turns every missing input into a silent guess, "
                           "the costliest quadrant in HiL-Bench. Replace it with: if a required value is missing, stop and ask the person.",
                    "evidence": res.guess_rules[:2]})
    if _count(res, "feedback-capture") and not learn:
        out.append({"title": "Keep the corrections you already collect",
                    "why": "People already correct or rate the output here, but nothing stores it for the next run, so the same mistake comes back. "
                           "Save each correction and add it to the prompt as an example or rule (tier 2, then tier 1): this is the cheapest jump in leverage.",
                    "evidence": _ev(res, "feedback-capture")})
    if not esc:
        out.append({"title": "Add an escalation path for blockers",
                    "why": "With no way to hand a case to a person, every missing or ambiguous input becomes a silent guess. "
                           "HiL-Bench found frontier agents rarely ask when they should. Escalate on low confidence or missing data, and only then.",
                    "evidence": ["no escalation, handoff or ask-the-human code found"]})
    if t4 >= 2 and t4 >= t1 + t2:
        out.append({"title": "Replace per-step approvals with rules and checks",
                    "why": "Approving every step is tier-4 review: one decision at a time, and the first place people start rubber-stamping. "
                           "Turn what the approver checks into a rule (tier 1) or a test (tier 2).",
                    "evidence": _ev(res, "per-step-approval")})
    if not learn and not _count(res, "feedback-capture"):
        out.append({"title": "Close the loop: feed results back",
                    "why": "The loop acts and measures but does not change itself. Store corrections and outcomes and use them to update prompts, rules or memory.",
                    "evidence": _ev(res, "feedback-capture") or ["no feedback or learning code found"]})
    if not res.test_files and not _count(res, "eval-set"):
        out.append({"title": "Add a small eval set",
                    "why": "Without expected outputs there is no way to tell if a change made the loop better. Ten real cases with the right answer are enough to start.",
                    "evidence": ["no tests/ or evals/ found"]})
    if not res.rule_files:
        out.append({"title": "Write the rules you keep repeating to the agent",
                    "why": "A CLAUDE.md or AGENTS.md turns instructions said in chat into rules that apply to every session (much higher leverage).",
                    "evidence": ["no CLAUDE.md / AGENTS.md / .cursorrules found"]})
    if not t3:
        out.append({"title": "Let a person review decisions as a set",
                    "why": "A log or dashboard of the loop's decisions lets someone spot patterns (tier 3) instead of checking cases one by one.",
                    "evidence": ["no monitoring, metrics or review-queue code found"]})
    return out[:4]


def report(res: RepoResult) -> str:
    bar = lambda v: "█" * round(max(0, min(1, v)) * 20) + "·" * (20 - round(max(0, min(1, v)) * 20))
    if res.verdict.startswith("No AI"):
        return f"{res.root}\n  {res.verdict}\n  ({res.files} files scanned, nothing was executed.)"
    L = [f"SIPS (design estimate) {res.sips:5.1f} / 100   {res.root}",
         f"  {res.files} files scanned · {res.commits} commits ({res.ai_commits} co-authored by an AI agent) · nothing was executed\n",
         *( [f"  {res.verdict}\n"] if res.verdict else [] ),
         "Components"]
    for k, (name, v, why) in res.components.items():
        shown = v / 100 if k == "γ" else v
        L.append(f"  {k:<3} {name:<30} {bar(shown)} {v:6.2f}   {why}")
    L.append("\nWhere your solution puts the human")
    for sig, role, _, why in PATTERNS:
        ev = _ev(res, sig, 2)
        if ev:
            L.append(f"  {sig:<18} ({role}) {why}")
            for e in ev:
                L.append(f"      {e}")
    if res.sessions_sips is not None:
        L.append(f"\nHow you built it (from your agent sessions): SIPS {res.sessions_sips:.1f}  · run `sips-score analyze --project <name>` for detail")
    L.append("\nWhat would raise it")
    for i, f in enumerate(res.findings, 1):
        L.append(f"  {i}. {f['title']}\n     Why: {f['why']}")
        for e in f["evidence"][:2]:
            L.append(f"       · {e}")
    L.append("\nThis is a static estimate from code and history. It cannot see how people actually used the loop.")
    return "\n".join(L)


def askf1(rows: list[dict]) -> dict:
    """HiL-Bench's Ask-F1: harmonic mean of question precision and blocker recall.

    Each row is one scenario run: {"blocker": bool, "asked": bool, "question_ok": bool}.
    precision = good questions / all questions asked;  recall = blockers the loop asked about / all blockers.
    """
    asked = [r for r in rows if r.get("asked")]
    good = [r for r in asked if r.get("blocker") and r.get("question_ok", True)]
    blockers = [r for r in rows if r.get("blocker")]
    p = len(good) / len(asked) if asked else 0.0
    rcl = len(good) / len(blockers) if blockers else 0.0
    f1 = 2 * p * rcl / (p + rcl) if p + rcl else 0.0
    quad = {"good escalation": len(good),
            "over-ask (asked, no blocker)": sum(1 for r in asked if not r.get("blocker")),
            "silent guess (blocker, did not ask)": sum(1 for r in blockers if not r.get("asked")),
            "asked badly (blocker, poor question)": sum(1 for r in asked if r.get("blocker") and not r.get("question_ok", True))}
    return {"precision": p, "recall": rcl, "ask_f1": f1, "runs": len(rows), "breakdown": quad}
