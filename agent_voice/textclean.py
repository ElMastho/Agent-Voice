"""Turn an agent's markdown reply into something pleasant to hear."""

from __future__ import annotations

import re

from agent_voice.i18n import plural, tr

_FENCE = re.compile(r"```.*?(```|$)", re.S)
_HTML_TAG = re.compile(r"</?[a-zA-Z][^>]*>")
_IMAGE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_URL = re.compile(r"https?://\S+")
_INLINE_CODE = re.compile(r"`([^`\n]+)`")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s*", re.M)
_BULLET = re.compile(r"^\s*(?:[-*+•]|\d+[.)])\s+", re.M)
_QUOTE = re.compile(r"^\s*>\s?", re.M)
_HR = re.compile(r"^\s*([-*_]\s*){3,}$", re.M)
_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$", re.M)
_EMPHASIS = re.compile(r"(\*\*|__|\*|_|~~)(?=\S)(.+?)(?<=\S)\1")
# Windows or POSIX paths with at least one separator, optionally followed by :line.
_PATH = re.compile(r"(?:[A-Za-z]:)?(?:[\w.\-~]+[\\/])+([\w.\-]+)(?::\d+(?::\d+)?)?")
_SPACES = re.compile(r"[ \t]+")


def _inline_code(match: re.Match) -> str:
    code = match.group(1).strip()
    # Short identifiers read fine ("the `config.py` file"); long snippets don't.
    return code if len(code) <= 40 and "(" not in code and "=" not in code else ""


def _table_row(line: str) -> str:
    if line.count("|") < 2:
        return line
    cells = [c.strip() for c in line.strip().strip("|").split("|")]
    return ", ".join(c for c in cells if c) + "."


def clean(text: str) -> str:
    text = _FENCE.sub("\n", text)
    text = _HTML_TAG.sub("", text)
    text = _IMAGE.sub(r"\1", text)
    text = _LINK.sub(r"\1", text)
    text = _URL.sub("", text)
    text = _INLINE_CODE.sub(_inline_code, text)
    text = _TABLE_SEP.sub("", text)
    text = "\n".join(_table_row(line) for line in text.splitlines())
    text = _HR.sub("", text)
    text = _HEADING.sub("", text)
    text = _BULLET.sub("", text)
    text = _QUOTE.sub("", text)
    for _ in range(2):
        text = _EMPHASIS.sub(r"\2", text)
    text = _PATH.sub(r"\1", text)
    text = text.replace("→", ", ").replace("->", ", ").replace("=>", ", ")
    text = _SPACES.sub(" ", text)
    text = re.sub(r" +([,.;:!?])", r"\1", text)
    lines = [line.strip() for line in text.splitlines()]
    # Keep paragraph breaks (used for language segmentation), drop empty runs.
    out: list[str] = []
    for line in lines:
        if line or (out and out[-1]):
            out.append(line)
    return "\n".join(out).strip()


def paragraphs(text: str) -> list[str]:
    """Split cleaned text into paragraphs; list items become their own paragraph."""
    parts = re.split(r"\n\s*\n|\n", text)
    return [p.strip() for p in parts if re.search(r"\w", p)]


_SENTENCE = re.compile(r"(?<=[.!?…:;])\s+")


def sentences(paragraph: str) -> list[str]:
    return [s for s in _SENTENCE.split(paragraph) if re.search(r"\w", s)]


def truncate(text: str, max_chars: int) -> str:
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    cut = text[:max_chars]
    end = max(cut.rfind(". "), cut.rfind("\n"), cut.rfind("! "), cut.rfind("? "))
    return cut[: end + 1] if end > max_chars // 2 else cut



def result_label(result: dict | None) -> str:
    """{"files": 3, "tests": "passed"} -> '3 files changed · tests passed'."""
    if not result:
        return ""
    files = int(result.get("files") or 0)
    parts = [plural(files, "{n} file changed", "{n} files changed")] if files else []
    if result.get("tests"):
        parts.append(tr("tests {result}", result=tr(str(result["tests"]))))
    return " · ".join(parts)


# An agent out of quota: "5-hour limit reached ∙ resets 3pm", "Claude AI usage limit reached|1760000000",
# "You've hit your usage limit. Try again at 3:45 PM." Only short messages: a long reply that discusses
# rate limits is not one.
_LIMIT_HIT = re.compile(r"\b(?:usage|rate|session|weekly|5-hour|five-hour)\s+limit\s+(?:reached|hit|exceeded)|"
                        r"\b(?:hit|reached)\s+(?:your|the)\s+(?:\w+\s+)?(?:usage\s+)?limit\b|"
                        r"\blimite\s+(?:d'utilisation\s+)?atteinte\b", re.I)
_LIMIT_EPOCH = re.compile(r"limit reached\|(\d{10})")
_LIMIT_AT = re.compile(r"\b(?:resets?|try again|réinitialis\w*|réessayez)\s+(?:at\s+|à\s+|vers\s+)?"
                       r"(?P<h>\d{1,2})(?:[:h](?P<m>\d{2}))?\s*(?P<ap>am|pm|a\.m\.|p\.m\.)?", re.I)
_LIMIT_IN = re.compile(r"\b(?:in|dans)\s+(?P<n>\d+)\s*(?P<unit>h|hours?|heures?|min|minutes?)\b", re.I)


def limit_reset(text: str, now: float | None = None) -> float | None:
    """None: not a usage-limit message. Otherwise when it resets (epoch seconds), or 0.0 when it doesn't say."""
    import time

    from agent_voice.schedule import clock_hour, next_at

    text = " ".join((text or "").split())
    if not text or len(text) > 300:
        return None
    epoch = _LIMIT_EPOCH.search(text)
    if not epoch and not _LIMIT_HIT.search(text):
        return None
    if epoch:
        return float(epoch.group(1))
    now = time.time() if now is None else now
    m = _LIMIT_AT.search(text)
    at = next_at(clock_hour(int(m["h"]), m["ap"]), int(m["m"] or 0), now) if m else None
    if at:
        return at
    m = _LIMIT_IN.search(text)
    if m:
        n = int(m["n"])
        return now + n * (3600 if m["unit"].lower().startswith("h") else 60)
    return 0.0


def tokens_label(n: int) -> str:
    """1840000 -> '1.8M', 5200 -> '5k'."""
    return f"{n / 1e6:.1f}M" if n >= 1e6 else f"{round(n / 1e3)}k" if n >= 1e3 else str(n)


def expand_shortcut(text: str, shortcuts: dict) -> str | None:
    """A saved prompt said by its name ("run the tests", "please commit this") -> its full text; None otherwise."""
    said = " ".join(re.sub(r"[^\w\s']", " ", text.lower()).split())
    said = re.sub(r"^(?:please|can you|could you|now|ok|okay|alors|s'il te pla[iî]t)\s+", "", said)
    said = re.sub(r"\s+(?:please|now|s'il te pla[iî]t)$", "", said)
    for name, prompt in (shortcuts or {}).items():
        if said == " ".join(re.sub(r"[^\w\s']", " ", str(name).lower()).split()) and str(prompt).strip():
            return str(prompt).strip()
    return None
