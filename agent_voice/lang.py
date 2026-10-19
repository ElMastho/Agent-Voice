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
    """text -> (p_fr, p_en), or None when no statistical detector is installed.

    lingua is the most accurate but weighs 300 MB, so the release exe ships py3langid (2 MB) instead.
    """
    try:
        from lingua import Language, LanguageDetectorBuilder

        det = (
            LanguageDetectorBuilder.from_languages(Language.ENGLISH, Language.FRENCH)
            .with_preloaded_language_models()
            .build()
        )

        def lingua_scores(text: str) -> tuple[float, float]:
            conf = {c.language.iso_code_639_1.name.lower(): c.value for c in det.compute_language_confidence_values(text)}
            return conf.get("fr", 0.0), conf.get("en", 0.0)

        return lingua_scores
    except ImportError:
        pass
    try:
        from py3langid.langid import MODEL_FILE, LanguageIdentifier

        ident = LanguageIdentifier.from_model_file(MODEL_FILE, norm_probs=True)
        ident.set_languages(["fr", "en"])

        def langid_scores(text: str) -> tuple[float, float]:
            ranked = dict(ident.rank(text))
            return ranked.get("fr", 0.0), ranked.get("en", 0.0)

        return langid_scores
    except ImportError:
        return None


def _heuristic(text: str) -> str | None:
    fr, en = len(_FR_HINTS.findall(text)), len(_EN_HINTS.findall(text))
    if fr == en:
        return None
    return "fr" if fr > en else "en"


_PREFERRED = "en"  # your language (Settings): what an unsure detection falls back to


def prefer(code: str) -> None:
    global _PREFERRED
    _PREFERRED = code if code in ("fr", "en") else "en"


def detect(text: str, fallback: str | None = None) -> str:
    fallback = fallback or _PREFERRED
    words = len(text.split())
    short = words < 4
    if short and _heuristic(text):
        return _heuristic(text)
    det = _detector()
    if det is None:
        return _heuristic(text) or fallback
    fr, en = det(text)
    winner = "fr" if fr > en else "en"
    # Short phrases need a clearer margin before we trust the statistics, and more again to beat your language.
    if abs(fr - en) < (0.4 if short else 0.2) + (0.2 if winner != _PREFERRED else 0.0):
        return _heuristic(text) or fallback
    return winner


def segment(paragraphs: list[str], default: str | None = None) -> list[tuple[str, str]]:
    """Return [(lang, unit)] for paragraphs or sentences; short ambiguous ones inherit the previous language."""
    default = default or _PREFERRED
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
