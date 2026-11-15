"""Mask secrets before any of your text is printed or saved. Reports get pasted into chats; credentials must not."""
import re

_PATTERNS = [
    (re.compile(r"\b(sk|pk|rk|ubx|ghp|gho|ghs|xox[abpr]|glpat|hf|AKIA)[-_][A-Za-z0-9_\-]{8,}"), "[key]"),
    (re.compile(r"\bBearer\s+[A-Za-z0-9._\-]{10,}", re.I), "Bearer [token]"),
    (re.compile(r"\b[A-Za-z0-9+/=_\-]{32,}\b"), "[token]"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"), "[email]"),
    (re.compile(r"(?i)\b(pass(word)?|pwd|contrase[ñn]a|clave|token|secret|api[ _-]?key|user(name)?|usuario)(\s*(es|is|=|:)\s*)(\S+)"), r"\1\4[hidden]"),
]


def redact(text: str) -> str:
    for rx, repl in _PATTERNS:
        text = rx.sub(repl, text)
    return text
