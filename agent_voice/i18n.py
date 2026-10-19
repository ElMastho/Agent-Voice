"""The app's own words in English or French (Settings > Language > Interface; what the voice says follows Speech).

Strings are written in English where they are used and looked up here: `tr("Settings")`; with values,
`tr("{n} need you", n=2)`; a word that depends on where it is, with a context: `tr("theme|Light")`; counted ones with `plural(n, "{n} turn", "{n} turns")`; a phrase for the voice in the
speech language with `tr(text, in_lang=...)`. The French lives in `i18n_fr.FR`, keyed by the English (a test checks
that every string passed to tr() has its French). Module-level constants are translated where they are shown, not
where they are defined: the language is known only once the app runs.

stdlib-only: textclean (imported by the hooks) uses it.
"""

from __future__ import annotations

from agent_voice.i18n_fr import FR

LANG = "en"  # the interface's


def set_language(code: str) -> None:
    global LANG
    LANG = "fr" if code == "fr" else "en"


def tr(text: str, /, *, in_lang: str | None = None, **values) -> str:
    """`text` may start with a context ("theme|Light"): the same English word, translated differently there."""
    if (in_lang or LANG) == "fr":
        text = FR.get(text, text)
    text = text.split("|", 1)[-1]
    return text.format(**values) if values else text


def plural(n: int, one: str, many: str, /, *, in_lang: str | None = None, **values) -> str:
    """French counts 0 and 1 as singular, English only 1."""
    lang = in_lang or LANG
    return tr(one if (n <= 1 if lang == "fr" else n == 1) else many, in_lang=lang, n=n, **values)


# What an agent is doing ("run pytest", "edit widget.py": hooks.describe_tool), its verb in French.
_ACTIONS_FR = (("run ", "exécuter "), ("edit a file", "modifier un fichier"), ("edit ", "modifier "),
               ("read a file", "lire un fichier"), ("read ", "lire "), ("look up the web", "chercher sur le web"),
               ("look up ", "chercher "), ("use ", "utiliser "), ("thinking", "réflexion"))


def action(text: str, *, in_lang: str | None = None) -> str:
    """An agent's action ("Run pytest") in the language: the verb translated, the rest (a command, a file) kept."""
    if (in_lang or LANG) != "fr":
        return text
    low = text.lower()
    for en, fr in _ACTIONS_FR:
        if low.startswith(en):
            out = fr + text[len(en):]
            return out[:1].upper() + out[1:] if text[:1].isupper() else out
    return text


_DAYS_FR = ("lun.", "mar.", "mer.", "jeu.", "ven.", "sam.", "dim.")


def day_name(when) -> str:
    """A datetime's short weekday: 'Mon', 'lun.' (strftime's %a is always English here)."""
    return _DAYS_FR[when.weekday()] if LANG == "fr" else when.strftime("%a")


def day_time(when) -> str:
    """'Mon 09:00', 'lun. 09:00'."""
    return f"{day_name(when)} {when.strftime('%H:%M')}"
