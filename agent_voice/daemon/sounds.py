"""Cues and chimes: soft tones played on a stream of their own, so they never cut the voice off."""

from __future__ import annotations

import logging
import threading

from agent_voice import devices

log = logging.getLogger("agent_voice.server")


def _beep(freq: int) -> None:
    """A short, soft cue (the mic opens, it heard you, it didn't): a gentle tone, not the system's square-wave beep.

    High asks rise (the mic opens, yes), low ones fall (nothing heard, no, not sent); played on a stream of
    its own, so a cue never cuts the voice off.
    """
    if freq >= 800:
        notes = ((freq * 0.75, 0.0), (freq, 0.06))  # up: listening, done, yes
    elif freq >= 500:
        notes = ((freq, 0.0),)
    else:
        notes = ((max(freq, 330.0) * 1.25, 0.0), (max(freq, 330.0), 0.07))  # down: nothing, no, not sent
    try:
        audio, rate = tone_audio(notes, 0.16, decay=14.0, tail=0.18)
    except Exception:
        return
    threading.Thread(target=_play_apart, args=(audio, rate), daemon=True).start()


def _play_apart(audio, rate: int) -> None:
    """Play on a stream of its own (sounddevice's play() would stop the voice playing through it)."""
    try:
        import sounddevice as sd

        with sd.OutputStream(samplerate=rate, channels=1, dtype="float32", device=devices.output_device()) as stream:
            stream.write(audio.reshape(-1, 1))
    except Exception:
        log.debug("cue not played", exc_info=True)


# Chime mode: (frequency Hz, start s) notes, a soft mallet sound. "done" settles on a fifth (C5, G5); "attention"
# asks, a little higher and twice (G5 then C6, repeated).
CHIMES = {
    "done": ((523.25, 0.0), (783.99, 0.11)),
    "attention": ((783.99, 0.0), (1046.5, 0.12), (783.99, 0.42), (1046.5, 0.54)),
}


def tone_audio(notes, volume: float = 0.35, rate: int = 44100, decay: float = 7.0, tail: float = 0.6):
    """Soft mallet tones: a sine with a gentle attack, a warm low partial that fades first, a smooth tail.

    No harmonics above the note and no clicks (eased in and out): nothing shrill, even on laptop speakers.
    """
    import numpy as np

    length = notes[-1][1] + tail
    t = np.arange(int(rate * length)) / rate
    out = np.zeros_like(t, dtype=np.float32)
    for freq, start in notes:
        u = t - start
        on = u >= 0
        uu = np.clip(u, 0, None)
        attack = np.minimum(uu / 0.012, 1.0) ** 2
        body = np.sin(2 * np.pi * freq * uu) * np.exp(-uu * decay)
        warmth = 0.18 * np.sin(np.pi * freq * uu) * np.exp(-uu * decay * 2.2)  # an octave below, brief
        out += np.where(on, attack * (body + warmth), 0.0).astype(np.float32)
    fade = int(rate * 0.03)
    out[-fade:] *= np.linspace(1.0, 0.0, fade, dtype=np.float32)
    peak = float(np.max(np.abs(out))) or 1.0
    return (out / peak * max(0.0, min(1.0, volume))).astype(np.float32), rate


def chime_audio(kind: str, volume: float = 0.35, rate: int = 44100):
    return tone_audio(CHIMES.get(kind, CHIMES["done"]), volume, rate)
