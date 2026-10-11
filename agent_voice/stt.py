"""Push-to-talk dictation with faster-whisper (local, CPU), French/English auto-detected."""

from __future__ import annotations

import logging
import queue
import threading

import numpy as np
import sounddevice as sd

log = logging.getLogger("agent_voice.stt")

RATE = 16000


def rms(block: np.ndarray) -> float:
    return float(np.sqrt(np.mean(block**2))) if block.size else 0.0


class Recorder:
    def __init__(self, on_level=None):
        self._frames: list[np.ndarray] = []
        self._stream: sd.InputStream | None = None
        self._on_level = on_level

    def start(self) -> None:
        self._frames = []
        self._stream = sd.InputStream(samplerate=RATE, channels=1, dtype="float32", callback=self._cb)
        self._stream.start()

    def _cb(self, indata, frames, time, status):  # noqa: ARG002 - sounddevice signature
        self._frames.append(indata.copy())
        if self._on_level:
            self._on_level(rms(indata))

    def stop(self) -> np.ndarray:
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None
        if not self._frames:
            return np.zeros(0, dtype=np.float32)
        return np.concatenate(self._frames)[:, 0]


class Transcriber:
    def __init__(self, config: dict):
        self.cfg = config["stt"]
        self._model = None
        self._lock = threading.Lock()

    def _load(self):
        with self._lock:
            if self._model is None:
                from faster_whisper import WhisperModel

                log.info("loading whisper model %s", self.cfg["model"])
                self._model = WhisperModel(self.cfg["model"], device="cpu", compute_type=self.cfg["compute_type"])
            return self._model

    def warm_up(self) -> None:
        self._load()

    def transcribe(self, audio: np.ndarray) -> tuple[str, str]:
        """Return (text, language)."""
        if audio.size < RATE * 0.3:
            return "", ""
        model = self._load()
        allowed = self.cfg.get("languages") or None
        segments, info = model.transcribe(audio, beam_size=1, vad_filter=True)
        lang = info.language
        if allowed and lang not in allowed:
            # Whisper sometimes guesses a neighbouring language; force the likeliest allowed one.
            probs = dict(info.all_language_probs or [])
            lang = max(allowed, key=lambda code: probs.get(code, 0.0))
            segments, info = model.transcribe(audio, beam_size=1, vad_filter=True, language=lang)
        text = " ".join(s.text.strip() for s in segments).strip()
        return text, lang


# Whisper tends to "hear" these in silence or background noise.
_HALLUCINATIONS = {
    "", "merci", "merci.", "merci beaucoup.", "thank you.", "thank you", "thanks for watching!",
    "thanks for watching.", "you", "bye.",
    "sous-titres réalisés par la communauté d'amara.org", "...",
}


def is_hallucination(text: str) -> bool:
    return text.strip().lower() in _HALLUCINATIONS


def listen_for_speech(
    window: float, silence: float, max_seconds: float, cancel: threading.Event, on_level=None, on_start=None
) -> np.ndarray | None:
    """Open the mic for up to `window` seconds; if speech starts, record until `silence` seconds of quiet.

    Returns the recorded audio, or None if nobody spoke (or it was cancelled).
    Energy-based detection with an adaptive noise floor; Whisper's VAD cleans up afterwards.
    """
    block = int(RATE * 0.03)  # 30 ms
    blocks: "queue.Queue[np.ndarray]" = queue.Queue()

    def cb(indata, frames, time_info, status):  # noqa: ARG001
        blocks.put(indata[:, 0].copy())

    with sd.InputStream(samplerate=RATE, channels=1, dtype="float32", blocksize=block, callback=cb):
        # Calibrate on ~300 ms of room noise.
        calib = [blocks.get(timeout=2) for _ in range(10)]
        floor = float(np.median([np.sqrt(np.mean(b**2)) for b in calib]))
        threshold = max(floor * 3.0, 0.008)
        preroll: list[np.ndarray] = list(calib[-10:])
        recorded: list[np.ndarray] = []
        loud_run = 0
        started = False
        quiet = 0.0
        elapsed = 0.0
        while not cancel.is_set():
            try:
                b = blocks.get(timeout=1)
            except queue.Empty:
                return None
            elapsed += 0.03
            level = rms(b)
            if on_level:
                on_level(level)
            loud = level > threshold
            if not started:
                preroll = (preroll + [b])[-10:]
                loud_run = loud_run + 1 if loud else 0
                if loud_run >= 4:  # ~120 ms of sound: someone is talking
                    started, recorded, elapsed = True, list(preroll), 0.0
                    if on_start:
                        on_start()
                elif elapsed >= window:
                    return None
                continue
            recorded.append(b)
            quiet = 0.0 if loud else quiet + 0.03
            if quiet >= silence or elapsed >= max_seconds:
                return np.concatenate(recorded)
    return None
