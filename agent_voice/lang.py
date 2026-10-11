"""French / English detection, paragraph by paragraph."""

from __future__ import annotations

import re
from functools import lru_cache

# Cheap fallback used for very short snippets, where statistical detection is unreliable.
_FR_HINTS = re.compile(
    r"\b(le|la|les|des|une|est|et|je|tu|nous|vous|pas|que|qui|pour|avec|dans|sur|ça|c'est|j'ai|"
    r"oui|non|merci|bonjour|voilà|fichier|fait|maintenant|aussi)\b|[éèêàùçôîœ]",
    re.I,
)
_EN_HINTS = re.compile(
    r"\b(the|and|is|are|you|i|we|it|this|that|with|for|not|have|has|done|now|file|also|will)\b",
    re.I,
)


@lru_cache(maxsize=1)
def _detector():
    try:
        from lingua import Language, LanguageDetectorBuilder
    except ImportError:
        return None
    return (
        LanguageDetectorBuilder.from_languages(Language.ENGLISH, Language.FRENCH)
        .with_preloaded_language_models()
        .build()
    )


def _heuristic(text: str) -> str | None:
    fr, en = len(_FR_HINTS.findall(text)), len(_EN_HINTS.findall(text))
    if fr == en:
        return None
    return "fr" if fr > en else "en"


def detect(text: str, fallback: str = "en") -> str:
    words = len(text.split())
    short = words < 4
    if short and _heuristic(text):
        return _heuristic(text)
    det = _detector()
    if det is None:
        return _heuristic(text) or fallback
    conf = {c.language.iso_code_639_1.name.lower(): c.value for c in det.compute_language_confidence_values(text)}
    fr, en = conf.get("fr", 0.0), conf.get("en", 0.0)
    # Short phrases need a clearer margin before we trust the statistics.
    if abs(fr - en) < (0.4 if short else 0.2):
        return _heuristic(text) or fallback
    return "fr" if fr > en else "en"


def segment(paragraphs: list[str], default: str = "en") -> list[tuple[str, str]]:
    """Return [(lang, unit)] for paragraphs or sentences; short ambiguous ones inherit the previous language."""
    # Detect the reply's dominant language first so it can be the fallback.
    dominant = detect(" ".join(paragraphs)[:2000], default) if paragraphs else default
    out: list[tuple[str, str]] = []
    prev = dominant
    for p in paragraphs:
        lang = detect(p, prev)
        out.append((lang, p))
        prev = lang
    return out


def warm_up() -> None:
    _detector()
