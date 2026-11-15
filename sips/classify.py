"""Assign each human turn to a SIPS tier, with the rule and the words that triggered it.

Tiers (Baena, Torres & Chíncaro, BiAlign@CHI'26):
  1 Strategic control   goals, constraints, specs, rules that persist
  2 Calibration         tests, examples, missing context, corrections with a reason
  3 Informed oversight  noticing patterns across outputs, reviewing a set
  4 Mechanical review   approving, continuing, fixing one output, interrupting

Rules are plain regexes (English and Spanish) so every assignment can be explained and argued with.
Improve them by adding labelled turns with `sips-score label` and running `sips-score eval`.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .model import HumanTurn

# Files whose edits persist across sessions: rules, memory, hooks, settings.
PERSIST_FILE = re.compile(r"(CLAUDE\.md|AGENTS\.md|GEMINI\.md|\.cursorrules|\.cursor/rules|copilot-instructions\.md|"
                          r"/memory/|MEMORY\.md|settings(\.local)?\.json|/hooks?/|\.claude/(commands|skills|agents)/)", re.I)
TEST_FILE = re.compile(r"(^|/)(tests?|spec|__tests__)/|(_test|\.test|\.spec|test_)[^/]*\.(py|ts|tsx|js|go|rs|rb)$", re.I)


def _rx(*words: str) -> re.Pattern:
    return re.compile(r"(?<!\w)(" + "|".join(words) + r")(?!\w)", re.I)


RULES: list[tuple[str, int, re.Pattern, str]] = [
    # id, tier, pattern, why it matters
    ("persist-request", 1, _rx(r"from now on", r"remember (that|to)", r"de ahora en adelante", r"a partir de ahora", r"recuerda (que|siempre)",
                              r"guarda(lo)? en (la )?memoria", r"add (this|it) to (claude|agents)\.md", r"always do", r"never do", r"in future sessions"),
     "asks the agent to keep a rule for future work"),
    ("constraint", 1, _rx(r"never", r"nunca", r"must not", r"don'?t (ever )?(touch|modify|change|delete|push|commit)", r"no (toques|modifiques|cambies|borres|hagas commit)",
                          r"sin (tocar|cambiar)", r"only (use|touch|change)", r"solo (usa|toca|cambia)", r"must", r"tiene que", r"hard constraint"),
     "sets a boundary the agent has to respect"),
    ("plan", 1, _rx(r"antes de (codear|programar|modificar|implementar|tocar|escribir)", r"before (coding|you code|implementing|changing|writing)", r"pensemos", r"let'?s think",
                    r"por fases", r"fasear\w*", r"in phases", r"step by step plan", r"development plan\w*", r"planning", r"underspecified", r"asegu?r[aá]te de que", r"make sure (that|we|you)",
                    r"trabajamos con", r"we (work|should work) with", r"deber[ií]amos (usar|hacer|tener|poner)", r"we should (use|do|have|put)", r"podr[ií]amos (poner|usar|mover|cambiar)"),
     "decides the approach before the agent produces work"),
    ("spec", 1, _rx(r"requirements?", r"requisitos?", r"spec(ification)?", r"architecture", r"arquitectura", r"the goal is", r"el objetivo es",
                    r"acceptance criteria", r"criterios? de aceptaci[oó]n", r"design (it|this) (so|as)", r"plan( first)?", r"interface", r"contract"),
     "defines what to build before it is built"),
    ("test-or-example", 2, _rx(r"(add|write|crea|añade|escribe) (a |un |unos? )?tests?", r"test case", r"caso de prueba", r"for example", r"por ejemplo", r"e\.g\.",
                               r"should return", r"deber[ií]a devolver", r"expected output", r"salida esperada", r"edge case", r"caso l[ií]mite"),
     "gives a test or example the agent can reuse"),
    ("reasoned-correction", 2, _rx(r"because", r"porque", r"ya que", r"the reason", r"la raz[oó]n", r"that'?s wrong since", r"the problem is", r"el problema es"),
     "corrects with a reason, which generalises beyond this output"),
    ("context", 2, _rx(r"context", r"contexto", r"for reference", r"background", r"note that", r"ten en cuenta", r"fyi", r"the data", r"los datos", r"according to"),
     "supplies information the agent did not have"),
    ("context-statement", 2, _rx(r"est[aá] (aqu[ií]|en)", r"it'?s (here|in|at)", r"hemos (usado|hecho|probado)", r"we (used|tried|did)", r"ya (no )?(se usa\w*|est[aá]|existe)",
                                 r"no (longer|more) used", r"terminaron", r"soy yo", r"(that'?s|it'?s) me", r"https?://", r"(?<![\w/])[\w.-]+/[\w.-]+(?![\w/])"),
     "states a fact the agent needed"),
    ("report", 2, _rx(r"me sale", r"i get", r"i'?m getting", r"error \d+", r"\b[45]\d\d\b", r"no (carga|arranca|conecta|engancha)", r"doesn'?t (load|start|connect)",
                      r"ya (he|la|lo|las|los) \w+", r"i (already|just) \w+ed", r"(estos|these) son (los|las|the)", r"es (el|la|un|una) "),
     "reports what happened or what is there"),
    ("quoted-example", 2, re.compile(r"(f[ií]jate en|look at|like this|como este|m[aá]s de este tipo|more like)[^\n]{0,80}[\"“]", re.I),
     "points to an example of what good looks like"),
    ("diagnosis", 2, _rx(r"no se (entiende|ve|lee)", r"(is|are)n'?t (clear|readable|visible)", r"no hace falta", r"no need (to|for)", r"falta(n)?", r"missing", r"hay que",
                         r"tiene(n)? q(ue)? (ser|tener|ir)", r"needs? to (be|have|go)", r"deber[ií]a(n)? (ser|ir|estar)", r"should (be|go|show)"),
     "says what is wrong or what the output must be"),
    ("pattern", 3, _rx(r"(you|it) .{0,20}again", r"otra vez (lo mismo|el mismo|la misma)", r"de nuevo (el|la|lo) mismo", r"keeps? (doing|making|adding|putting|using)", r"sigues", r"every time", r"cada vez", r"you always", r"siempre (haces|pones|olvidas)",
                       r"still (wrong|broken|failing|not|the same)", r"todav[ií]a (no|mal|falla|est[aá] mal)", r"same (mistake|error)", r"mismo error", r"repet\w+", r"se repite", r"redundan\w+", r"sigue siendo"),
     "points at a recurring pattern across outputs"),
    ("probe", 3, _rx(r"why", r"por qu[eé]", r"how (does|did|would)", r"c[oó]mo (funciona|has|lo)", r"what if", r"qu[eé] pasa si", r"explain", r"expl[ií]ca", r"are you sure", r"seguro que"),
     "questions the output instead of accepting it"),
    ("review-set", 3, _rx(r"review\w*", r"revis[aeo]\w*", r"comprueba (si|que)", r"verifica (si|que)", r"check (all|every|the whole)", r"audit", r"compare", r"compara",
                          r"look over", r"go through"),
     "reviews a set of outputs instead of one"),
    ("approve", 4, _rx(r"ok(ay)?", r"yes", r"s[ií]", r"vale", r"dale", r"go( ahead)?", r"proceed", r"contin[uú]a", r"continue", r"perfect", r"perfecto", r"great",
                       r"genial", r"lgtm", r"do it", r"hazlo", r"adelante", r"thanks?", r"gracias", r"done", r"commit", r"push", r"try again", r"sigue( con)?", r"keep going", r"sube a main"),
     "approves or continues without new information"),
    ("local-fix", 4, _rx(r"fix (this|that|it|the)", r"arregla", r"corrige", r"change .{1,40} to", r"cambia .{1,40} por", r"rename", r"renombra", r"typo",
                         r"wrong", r"mal", r"no funciona", r"doesn'?t work", r"not what i", r"eso no", r"undo", r"deshaz", r"revert", r"quita", r"remove (this|that)"),
     "fixes one output in place"),
]

# Second axis: what kind of feedback the person gives. Same tier, very different information.
# info = how much the agent (and the next session) can learn from it, 0..1.
FEEDBACK: list[tuple[str, float, re.Pattern, str]] = [
    ("restated", 0.1, _rx(r"i (literally |already )?(said|told you)", r"i asked you", r"ya te (lo )?(dije|he dicho|ped[ií])", r"te dije", r"como te dije", r"as i said"),
     "repeats an instruction the agent already ignored"),
    ("generalising", 1.0, _rx(r"whenever", r"siempre que", r"cada vez que", r"in general", r"en general", r"as a rule", r"por norma", r"from now on",
                              r"de ahora en adelante", r"for all", r"para todos"),
     "states a rule that covers future cases"),
    ("example", 0.9, _rx(r"for example", r"por ejemplo", r"e\.g\.", r"like this", r"as in", r"expected output", r"salida esperada", r"should return",
                         r"deber[ií]a devolver", r"test case"),
     "shows what good looks like"),
    ("diagnostic", 0.85, _rx(r"because", r"porque", r"ya que", r"the problem is", r"el problema es", r"the reason", r"caused by", r"se debe a", r"fails when", r"falla cuando"),
     "explains why it is wrong"),
    ("criteria", 0.8, _rx(r"should", r"deber[ií]a", r"must", r"tiene que", r"needs to", r"hay que", r"make sure", r"aseg[uú]rate", r"the standard", r"criteria", r"criterio"),
     "states the standard the output must meet"),
    ("context", 0.7, _rx(r"context", r"contexto", r"note that", r"ten en cuenta", r"fyi", r"for reference", r"the data", r"los datos", r"background", r"we use", r"usamos"),
     "adds information the agent lacked"),
    ("verify-request", 0.6, _rx(r"run (the )?tests?", r"ejecuta (los )?tests?", r"verify", r"verifica", r"comprueba", r"check (that|it|yourself)", r"prove", r"demuestra"),
     "asks the agent to check its own work"),
    ("question", 0.55, _rx(r"why", r"por qu[eé]", r"how (does|did|would)", r"c[oó]mo (funciona|has)", r"what if", r"qu[eé] pasa si", r"explain", r"expl[ií]ca"),
     "asks the agent to explain, which keeps the person reasoning"),
    ("directive", 0.35, _rx(r"change .{1,40} to", r"cambia .{1,40} por", r"replace", r"reemplaza", r"use .{1,30} instead", r"usa .{1,30} en (vez|lugar)", r"rename", r"renombra",
                            r"move .{1,30} to", r"mueve"),
     "dictates the exact edit; fixes this case only"),
    ("pattern-report", 0.5, _rx(r"(you|it) .{0,20}again", r"otra vez (lo mismo|el mismo|la misma)", r"keeps? (doing|making|adding|putting|using)", r"sigues", r"every time", r"same (mistake|error)", r"mismo error", r"still (wrong|broken|failing|not|the same)", r"todav[ií]a (no|mal|falla|est[aá] mal)"),
     "names a recurring failure, but not the fix"),
    ("outcome-only", 0.15, _rx(r"wrong", r"mal", r"no funciona", r"doesn'?t work", r"not working", r"broken", r"roto", r"not what i", r"eso no", r"no es (eso|as[ií])", r"try again", r"int[eé]ntalo de nuevo"),
     "says it failed without saying why"),
    ("acknowledge", 0.0, _rx(r"ok(ay)?", r"yes", r"s[ií]", r"vale", r"dale", r"go( ahead)?", r"proceed", r"contin[uú]a", r"continue", r"perfect", r"perfecto", r"thanks?",
                             r"gracias", r"lgtm", r"hazlo", r"do it"),
     "approves without new information"),
]
_SHOUT_WORDS = set("DONT DON'T NOT NO NEVER STOP SAID TOLD WHY PLEASE AGAIN YA DIJE NUNCA OTRA VEZ TOQUES".split())
FRUSTRATION_PHRASE = re.compile(r"!!+|\bI (literally|already) (said|told)\b|\bya te (lo )?(dije|he dicho)\b", re.I)


class _Frustration:
    """Shouting = three or more capitalised words in a row that include ordinary words (so 'ACM CHI IEEE' is not shouting)."""
    def search(self, text: str) -> bool:
        if FRUSTRATION_PHRASE.search(text):
            return True
        if len(text.split()) > 80:          # long prompts carry headings and titles in capitals
            return False
        for run in re.findall(r"(?:\b[A-Z][A-Z']+\b[ ,]+){2,}\b[A-Z][A-Z']+\b", text):
            words = re.findall(r"[A-Z']+", run)
            if len(words) >= 3 and any(w in _SHOUT_WORDS for w in words):
                return True
        return False


FRUSTRATION = _Frustration()


def feedback_type(text: str, kind: str = "prompt") -> tuple[str, float, str, str]:
    """Return (type, information 0..1, matched text, why). Picks the most informative cue present."""
    if kind == "interrupt":
        return ("interrupt", 0.1, "[interrupted]", "stopped the agent without saying why")
    if kind == "denial":
        return ("denial", 0.2, "[denied]", "refused a tool call")
    short = len(text.split()) <= 6
    for ftype, info, rx, why in FEEDBACK:
        if ftype == "acknowledge" and not short:
            continue
        m = rx.search(text)
        if m:
            return (ftype, info, m.group(0), why)
    n = len(text.split())
    info = 0.65 if n >= 80 else 0.5 if n >= 20 else 0.3
    return ("instruction", info, f"{n} words", "a request with no feedback cue; longer, specific requests carry more information")


CODE_HINT = re.compile(r"`[^`]+`|```|\w+\.(py|ts|tsx|js|go|rs|java|rb|md|json|ya?ml|sql)\b|\b\w+\(\)|/[\w.-]+/[\w.-]+")


@dataclass
class Assignment:
    tier: int
    confidence: float
    reasons: list[tuple[str, str, str]] = field(default_factory=list)   # (rule id, matched text, why)
    persisted: list[str] = field(default_factory=list)                 # files the agent wrote that persist
    tests_written: list[str] = field(default_factory=list)
    technical: bool = False
    feedback: str = ""
    info: float = 0.0
    feedback_why: tuple[str, str] = ("", "")
    frustrated: bool = False


def classify(turn: HumanTurn) -> Assignment:
    a = _tier(turn)
    a.feedback, a.info, matched, why = feedback_type(turn.text, turn.kind)
    a.feedback_why = (matched, why)
    a.frustrated = bool(FRUSTRATION.search(turn.text))
    return a


def _tier(turn: HumanTurn) -> Assignment:
    if turn.kind == "interrupt":
        return Assignment(4, 0.9, [("interrupt", "[interrupted]", "stopped the agent mid-task to redirect it")])
    if turn.kind == "denial":
        return Assignment(4, 0.9, [("denial", "[denied tool call]", "refused one tool call")])

    text = turn.text
    hits: dict[int, list[tuple[str, str, str]]] = {1: [], 2: [], 3: [], 4: []}
    for rid, tier, rx, why in RULES:
        m = rx.search(text)
        if m:
            hits[tier].append((rid, m.group(0), why))

    a = Assignment(4, 0.5, technical=bool(CODE_HINT.search(text)))
    # Evidence from what the agent did next: a rule file or a test written right after this turn.
    for act in turn.actions_after[:40]:
        if act.kind == "tool" and act.target and PERSIST_FILE.search(act.target):
            a.persisted.append(act.target)
        elif act.kind == "tool" and act.target and TEST_FILE.search(act.target):
            a.tests_written.append(act.target)
    asked_rule = bool(hits[1] or hits[2] or re.search(r"claude\.md|agents\.md|memor|hook|rule|regla|setting", text, re.I))
    if not asked_rule:
        a.persisted = []          # the agent edited a rules file on its own: not this person's intervention
    if a.persisted:
        hits[1].insert(0, ("persisted", a.persisted[0].split("/")[-1], "the agent wrote it into a file that applies to future sessions"))
    if a.tests_written and not hits[2]:
        hits[2].append(("test-written", a.tests_written[0].split("/")[-1], "a test was written, so the check repeats on every run"))

    words = len(text.split())
    short = words <= 6
    if "?" in text and not hits[1] and words <= 60:
        hits[3].append(("question", text[max(0, text.index("?") - 30):text.index("?") + 1].strip(), "questions the output instead of accepting it"))
    # A short message with only approval words is tier 4 whatever else matched.
    if short and hits[4] and not hits[1] and not a.persisted:
        return Assignment(4, 0.85, hits[4][:2], technical=a.technical)
    if any(h[0] == "question" for h in hits[3]) and not hits[1]:
        # questioning an output is oversight even when the question carries context
        return Assignment(3, 0.7, [h for h in hits[3] if h[0] == "question"] + hits[2][:1], a.persisted, a.tests_written, a.technical)
    for tier in (1, 2, 3):
        if hits[tier]:
            # "never/must" inside a one-off fix is weaker than a standing rule
            conf = 0.75 if tier != 1 or a.persisted or any(r[0] in ("persist-request", "spec") for r in hits[1]) else 0.6
            if words > 40:
                conf = min(0.9, conf + 0.1)
            return Assignment(tier, conf, hits[tier][:3], a.persisted, a.tests_written, a.technical)
    if hits[4]:
        return Assignment(4, 0.7, hits[4][:2], technical=a.technical)
    # No cue: long, specific instructions behave like a spec; short ones like a one-off request.
    if words >= 60:
        return Assignment(1, 0.4, [("long-instruction", f"{words} words", "a long, specific instruction usually defines the task up front")], technical=a.technical)
    if words >= 20:
        return Assignment(2, 0.35, [("medium-instruction", f"{words} words", "adds detail the agent did not have")], technical=a.technical)
    return Assignment(4, 0.35, [("short-request", f"{words} words", "a short one-off request about the current output")], technical=a.technical)
