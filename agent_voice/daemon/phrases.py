"""What you say, understood: commands, routing ("tell Codex to ..."), yes/no, numbers, the wake phrase."""

from __future__ import annotations

import logging
import re

log = logging.getLogger("agent_voice.server")


# Spoken answers to a question: "the second one", "option 2", "la deuxième"...
_ORDINALS = {
    1: ("1", "one", "first", "un", "une", "premier", "première", "premiere"),
    2: ("2", "two", "second", "deux", "deuxième", "deuxieme", "seconde"),
    3: ("3", "three", "third", "trois", "troisième", "troisieme"),
    4: ("4", "four", "fourth", "quatre", "quatrième", "quatrieme"),
    5: ("5", "five", "fifth", "cinq", "cinquième", "cinquieme"),
}


_YES = {"yes", "yeah", "yep", "allow", "approve", "ok", "okay", "sure", "go", "oui", "ouais", "vas", "autorise",
        "autoriser", "accepte", "daccord", "valide"}


_NO = {"no", "nope", "deny", "refuse", "stop", "dont", "non", "refuser", "annule", "pas"}


def match_yes_no(text: str) -> bool | None:
    """A spoken permission answer: True (allow), False (deny), or None when it isn't clear."""
    words = set(_plain(text.replace("'", "")).split())
    yes, no = bool(words & _YES), bool(words & _NO)
    return None if yes == no else yes


# "Claude" as Whisper writes it across accents: Claude, Clod, Klöte, Clot, Cloud, Clawed...
_WAKE_FUZZY = re.compile(
    r"^\W*(?:\w+\W+){0,2}?(?:hey|hé|he|hi|ok|okay|eh|et|ey|hay|ay)\W+[ck]l(?:au|eau|aw|ou|oa|o|ö)"
    r"(?:de|d|te|tte|t|th|ed)?\b\W*",
    re.I,
)


def strip_wake(text: str, phrases: list[str]) -> str | None:
    """The command after a wake phrase near the start ("Hey Claude, run the tests" -> "run the tests").

    Up to two words may come first ("Uh, hey Claude..."), not more: "I told Claude" is no wake-up call.
    """
    for phrase in phrases:
        words = [re.escape(w) for w in phrase.split()]
        m = re.match(r"^\W*(?:\w+\W+){0,2}?" + r"\W+".join(words) + r"\b\W*", text, re.I)
        if m:
            return text[m.end():].strip()
    m = _WAKE_FUZZY.match(text)
    return text[m.end():].strip() if m else None


# Said on their own in the reply window, these drive agent-voice instead of being sent to the agent.
_COMMANDS = {
    "repeat": re.compile(r"^(?:repeat|repeat that|say (?:that|it) again|again|what did you say|"
                         r"r[ée]p[èe]te|r[ée]p[èe]te[- ]?le|tu peux r[ée]p[ée]ter)$"),
    "slower": re.compile(r"^(?:slower|slow down|speak slower|plus lentement|moins vite|ralentis)$"),
    "faster": re.compile(r"^(?:faster|speed up|speak faster|plus vite|acc[ée]l[èe]re)$"),
    "cancel": re.compile(r"^(?:cancel|never ?mind|forget it|nothing|annule|laisse tomber|rien)$"),
}


def match_command(text: str) -> str | None:
    """'Repeat that.' -> 'repeat'; anything longer than a few words is a reply, never a command."""
    plain = re.sub(r"[^\w\s'-]", "", text.lower()).strip()
    plain = re.sub(r"^(?:please|s'il te pla[iî]t|ok|okay)\s+|\s+(?:please|s'il te pla[iî]t)$", "", plain)
    if not plain or len(plain.split()) > 5:
        return None
    return next((name for name, rx in _COMMANDS.items() if rx.match(plain)), None)


# "Tell Codex to run the tests": a message for another conversation, said from anywhere.
_ROUTES = (
    re.compile(r"^(?:please\s+)?(?:tell|ask)\s+(?P<name>.+?)\s+(?:to|that)\s+(?P<msg>.+)$", re.I | re.S),
    re.compile(r"^(?:send|message|write)\s+(?:to\s+)?(?P<name>[^,:]+?)\s*[,:]\s*(?P<msg>.+)$", re.I | re.S),
    re.compile(r"^(?:dis|demande)\s+(?:à|a)\s+(?P<name>.+?)\s+(?:de\s+|d'|d’|que\s+|qu'|qu’)(?P<msg>.+)$", re.I | re.S),
)


# Words that name nothing in particular: "approve the agent" must not pick a conversation.
_FILLER = {"the", "a", "an", "le", "la", "les", "l", "agent", "thread", "conversation", "one", "code", "please"}


# "New thread: fix the login bug", "start a new conversation with Codex to ...", "nouvelle session : ..."
_NEW_THREAD = re.compile(
    r"^(?:please\s+)?(?:start\s+)?(?:a\s+)?(?:new|nouvelle|nouveau)\s+(?:thread|conversation|session|chat|discussion|fil)"
    r"(?:\s+(?:with|in|avec|dans)\s+(?P<agent>claude(?:\s+code)?|codex|gemini))?\s*[:,.]?\s*(?:to\s+|pour\s+)?"
    r"(?P<prompt>.+)$", re.I | re.S)


NEW_THREAD = "__new__"  # the "session" a dictated new-thread prompt goes to


COMPANION = "__companion__"  # ... and what's said into the companion's mic


def parse_new_thread(text: str) -> tuple[str, str] | None:
    """'New thread with Codex: add a health check' -> ('codex', 'Add a health check'); None otherwise."""
    m = _NEW_THREAD.match(text.strip())
    if not m or len(m["prompt"].split()) < 2:
        return None
    agent = (m["agent"] or "").lower().split()[0] if m["agent"] else ""
    prompt = m["prompt"].strip()
    return agent, prompt[:1].upper() + prompt[1:]


def _new_target(agent: str) -> str:
    """Where a dictated new thread starts: the agent it names; Claude (or none) means where you last worked."""
    return "" if agent in ("", "claude") else agent


def _voice_line(text: str) -> str:
    """The `Voice summary:` line an agent ended its reply with, '' when it wrote none."""
    m = re.search(r"voice summary:\s*(.+)", text or "", re.I)
    return m.group(1).strip() if m else ""


def parse_route(text: str) -> tuple[str, str] | None:
    """'Tell Codex to run the tests.' -> ('Codex', 'run the tests.'); None for anything else."""
    text = text.strip()
    for rx in _ROUTES:
        m = rx.match(text)
        if m and 0 < len(m["name"].split()) <= 4 and m["msg"].strip(" .!?,"):
            msg = m["msg"].strip()
            return m["name"].strip(" ,"), msg[:1].upper() + msg[1:]
    return None


# "Send this to Codex", "give the screenshot to the notch one and ask it to fix it", "envoie ça à Codex : ..."
_HANDOFF = re.compile(
    r"^(?:please\s+)?(?:send|give|pass|share|hand|envoie|donne|passe|transmets)\s+"
    r"(?:this|that|it|the selection|my selection|the screenshot|this screenshot|my screenshot|ça|ca|ceci|cela|"
    r"la sélection|la selection|la capture)\s+(?:over\s+)?(?:to|à|a)\s+(?P<rest>.+)$", re.I | re.S)


_HANDOFF_ASK = re.compile(r"^(?:ask|tell)\s+(?:it|him|her|them)\s+to\s+|^(?:demande-lui|dis-lui)\s+(?:de\s+|d')?",
                          re.I)


def parse_handoff(text: str) -> tuple[str, str] | None:
    """'Send this to Codex and ask it to fix it' -> ('Codex', 'Fix it'); 'Send this to Codex' -> ('Codex', '')."""
    m = _HANDOFF.match(text.strip())
    if not m:
        return None
    parts = re.split(r"\s*[,:]\s*|\s+(?:and|et)\s+", m["rest"].strip(), maxsplit=1)
    name = parts[0].strip(" .!?")
    msg = _HANDOFF_ASK.sub("", parts[1].strip()) if len(parts) > 1 else ""
    if not name or len(name.split()) > 4:
        return None
    return name, msg[:1].upper() + msg[1:]


# "Resume when it resets", "continue Codex after the limit", "reprends quand la limite est levée"
_RESUME = re.compile(r"\b(?:resume|continue|carry on|keep going|reprends|reprendre|continuer?)\b.*"
                     r"\b(?:resets?|limit|limits|quota|limite|réinitialis\w*|reinitialis\w*)\b", re.I | re.S)


_RECAP = re.compile(r"^(?:give me\s+)?(?:the\s+|my\s+|today'?s\s+)?(?:daily\s+recap|recap(?: of (?:the|my) day)?|"
                    r"what did i do today|what happened today|day summary|summary of (?:the|my) day|"
                    r"résumé de (?:la|ma) journée|resume de (?:la|ma) journee|bilan de (?:la|ma) journée)\W*$", re.I)


# Said while a dictated message waits to be sent: keep it to fix it, drop it, or send it now.
_TAKE_BACK = {
    "hold": re.compile(r"^(?:wait|hold on|hold it|hang on|stop|edit|edit it|let me (?:fix|edit|change) (?:it|that)|"
                       r"attends?|attendez|pas encore|modifie|je corrige)$"),
    "cancel": re.compile(r"^(?:cancel(?: that| it)?|never ?mind|don'?t send(?: it| that)?|scratch that|delete (?:it|that)|"
                         r"forget it|annule|annule ça|laisse tomber|n'envoie pas|oublie)$"),
    "send": re.compile(r"^(?:send(?: it)?(?: now)?|go|go ahead|yes|ok send it|envoie(?:-le)?|vas-y|oui)$"),
}


# Said over the voice to make it stop (and then correct it): not a reply in itself.
_INTERRUPT = re.compile(r"^(?:stop|stop it|stop talking|wait|no|no no|no wait|wait wait|hold on|hang on|enough|that's enough|"
                        r"shut up|quiet|be quiet|ok stop|okay stop|actually|arrête|arrête-toi|stop stop|attends|non|"
                        r"non attends|chut|tais-toi|ça suffit|en fait)$")


def match_interrupt(text: str) -> bool:
    """'No, wait!' -> True: you cut the voice off to say something else, so the mic opens for it."""
    plain = re.sub(r"[^\w\s'-]", "", text.lower().replace("’", "'"))
    return bool(_INTERRUPT.match(" ".join(plain.split())))


def match_take_back(text: str) -> str | None:
    """'Wait!' -> 'hold', 'cancel that' -> 'cancel', 'send it' -> 'send'; None for anything else (more words)."""
    plain = re.sub(r"[^\w\s'-]", "", text.lower().replace("’", "'")).strip()
    plain = re.sub(r"^(?:no|non|oh|uh|euh|please|ok|okay)\s+|\s+(?:please|s'il te pla[iî]t)$", "", plain)
    return next((name for name, rx in _TAKE_BACK.items() if rx.match(plain)), None)


# "Look at this", "check this error and tell me what's wrong", "regarde ça" -> a picture of the window in front.
_LOOK = (
    re.compile(r"^(?:please\s+)?(?:look at|have a look at|take a look at|check(?: out)?|see)\s+(?:this|that|my screen|"
               r"the screen|this window|this page|this error|that error|what i'?m looking at)\b(?P<rest>.*)$", re.I | re.S),
    re.compile(r"^(?:regarde|vois|jette un (?:œil|oeil) (?:à|a))\s+(?:ça|ca|ceci|cela|mon écran|l'écran|cette fenêtre|"
               r"cette page|cette erreur)\b(?P<rest>.*)$", re.I | re.S),
)


def parse_look(text: str) -> str | None:
    """'Look at this and tell me why it fails' -> 'Tell me why it fails'; 'look at this' -> ''; None otherwise."""
    text = text.strip()
    for rx in _LOOK:
        m = rx.match(text)
        if m:
            rest = re.sub(r"^[\s,.:;!?-]*(?:(?:and|et)\s+)?", "", m["rest"]).strip()
            return rest[:1].upper() + rest[1:]
    return None


def match_resume(text: str) -> bool:
    return bool(_RESUME.search(text)) and len(text.split()) <= 12


def match_recap(text: str) -> bool:
    return bool(_RECAP.match(_plain(text).replace(" s ", "'s ")) or _RECAP.match(text.strip().lower()))


_BRIEF = re.compile(r"^(?:give me\s+)?(?:the\s+|my\s+)?(?:morning\s+brief(?:ing)?|brief(?:ing)?|what'?s (?:on )?my day|"
                    r"what does my day look like|(?:my )?plan for today|(?:le |mon )?brief(?: du matin)?|"
                    r"(?:c'?est )?quoi (?:ma journée|mon programme)|(?:le |mon )?programme du jour)\W*$", re.I)


_JOIN = re.compile(r"^(?:please\s+)?(?:(?:join|open|start)\s+(?:the\s+|my\s+)?(?:next\s+)?(?:meeting|call|teams|zoom|meet)"
                   r"(?:\s+(?:call|meeting))?|(?:rejoin(?:dre|s)?|lance(?:r)?|ouvr(?:e|ir))\s+(?:la\s+|ma\s+)?"
                   r"(?:prochaine\s+)?(?:réunion|reunion|visio|teams|zoom))\W*$", re.I)


def match_brief(text: str) -> bool:
    """'Morning brief', "what's my day", 'brief du matin'."""
    return bool(_BRIEF.match(_plain(text).replace(" s ", "'s ")) or _BRIEF.match(text.strip().lower()))


def match_join(text: str) -> bool:
    """'Join my meeting', 'rejoins la réunion'."""
    return bool(_JOIN.match(" ".join(text.strip().split())))


def spoken_number(words: set[str], most: int) -> int | None:
    """'two', '2', 'the second one' -> 2 (up to `most`); 'one' alone only when nothing else is said."""
    found = [n for n, keys in _ORDINALS.items() if n <= most and any(k in words for k in keys)]
    if len(found) > 1:
        found = [n for n in found if n != 1 or not words & {"one", "un", "une"}]
    return found[0] if found else None


_ALL = {"all", "everything", "everyone", "both", "tout", "tous", "toutes", "les deux"}


def _names(label: str) -> set[str]:
    return {w for w in _plain(label).split() if w not in _FILLER and len(w) > 1}


def names_match(said: str, label: str) -> bool:
    """Does `said` ("codex", "the notch one") name the conversation called `label`?"""
    want, have = _names(said), _names(label)
    return bool(want) and want <= have


def _plain(text: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", text.lower()).split())


def match_option(text: str, options: list[str]) -> int | None:
    """Which option a spoken answer picks: its label said out loud, or its number. None otherwise."""
    said = _plain(text)
    labels = [_plain(re.sub(r"\(.*?\)", "", o)) for o in options]
    for i in sorted(range(len(labels)), key=lambda i: -len(labels[i])):
        if labels[i] and labels[i] in said:
            return i
    words = said.split()
    if len(words) <= 5:  # a short answer that names a position, not a sentence that happens to contain "one"
        n = spoken_number(set(words), len(options))
        if n:
            return n - 1
    return None
