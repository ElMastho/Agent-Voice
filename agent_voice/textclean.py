"""Turn an agent's markdown reply into something pleasant to hear."""

from __future__ import annotations

import re

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
