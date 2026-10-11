"""Piper-based speaker: synthesizes sentence by sentence while the previous one plays."""

from __future__ import annotations

import logging
import queue
import threading
import time

import numpy as np
import sounddevice as sd

from agent_voice import lang as langdet
from agent_voice import textclean
from agent_voice.config import VOICES_DIR

log = logging.getLogger("agent_voice.tts")


def download_voice(name: str) -> None:
    from piper.download_voices import download_voice as _download

    VOICES_DIR.mkdir(parents=True, exist_ok=True)
    _download(name, VOICES_DIR)


class Speaker:
    def __init__(self, config: dict):
        self.config = config
        self._voices: dict[str, object] = {}
        self._voices_lock = threading.Lock()
        self._gen = 0
        self._text_q: queue.Queue = queue.Queue()
        self._audio_q: queue.Queue = queue.Queue(maxsize=4)
        self._playing = threading.Event()
        self._synthesizing = threading.Event()
        self.current_spec = ""
        threading.Thread(target=self._synth_loop, name="synth", daemon=True).start()
        threading.Thread(target=self._play_loop, name="play", daemon=True).start()

    # -- voices -----------------------------------------------------------------
    def _spec(self, lang: str) -> str:
        return self.config["voices"].get(lang) or self.config["voices"][self.config["default_lang"]]

    def _voice(self, spec: str):
        """Return (PiperVoice, speaker_id). A voice spec may name a speaker: "en_GB-semaine-medium:obadiah"."""
        name, _, speaker = spec.partition(":")
        with self._voices_lock:
            if name not in self._voices:
                from piper import PiperVoice

                path = VOICES_DIR / f"{name}.onnx"
                if not path.exists():
                    log.info("downloading voice %s", name)
                    download_voice(name)
                self._voices[name] = PiperVoice.load(path)
            voice = self._voices[name]
        speaker_id = voice.config.speaker_id_map.get(speaker) if speaker else None
        return voice, speaker_id

    def warm_up(self) -> None:
        for lang in self.config["voices"]:
            self._voice(self._spec(lang))
        langdet.warm_up()

    # -- public API -------------------------------------------------------------
    def speak(self, text: str, lang: str | None = None, voice: str | None = None) -> int:
        """Queue text for speaking (optionally forcing a voice spec). Returns the number of sentences queued."""
        cleaned = textclean.truncate(textclean.clean(text), int(self.config["max_chars"]))
        paras = textclean.paragraphs(cleaned)
        if not paras:
            return 0
        units = [s for p in paras for s in textclean.sentences(p)]
        segments = [(lang, s) for s in units] if lang else langdet.segment(units, self.config["default_lang"])
        gen = self._gen
        for seg_lang, sentence in segments:
            self._text_q.put((gen, voice or self._spec(seg_lang), sentence))
        return len(segments)

    def stop(self) -> None:
        self._gen += 1
        for q in (self._text_q, self._audio_q):
            try:
                while True:
                    q.get_nowait()
            except queue.Empty:
                pass
        sd.stop()

    @property
    def busy(self) -> bool:
        return self._playing.is_set() or self._synthesizing.is_set() or not self._text_q.empty() or not self._audio_q.empty()

    # -- workers ----------------------------------------------------------------
    def _synth_loop(self) -> None:
        from piper import SynthesisConfig

        while True:
            gen, spec, sentence = self._text_q.get()
            if gen != self._gen:
                continue
            self._synthesizing.set()
            try:
                voice, speaker_id = self._voice(spec)
                syn = SynthesisConfig(
                    speaker_id=speaker_id, length_scale=float(self.config["speed"]), volume=float(self.config["volume"])
                )
                chunks = list(voice.synthesize(sentence, syn_config=syn))
                if not chunks or gen != self._gen:
                    continue
                audio = np.concatenate([c.audio_float_array for c in chunks]).astype(np.float32)
                # A short pause between sentences sounds more natural.
                audio = np.concatenate([audio, np.zeros(int(chunks[0].sample_rate * 0.12), dtype=np.float32)])
                self._audio_q.put((gen, audio, chunks[0].sample_rate, spec))
            except Exception:  # never let one bad sentence kill the worker
                log.exception("synthesis failed for %r", sentence)
            finally:
                self._synthesizing.clear()

    def _play_loop(self) -> None:
        while True:
            gen, audio, rate, spec = self._audio_q.get()
            if gen != self._gen:
                continue
            self.current_spec = spec
            self._playing.set()
            try:
                sd.play(audio, rate)
                # Poll instead of sd.wait(): a broken audio driver must not freeze the speaker forever.
                deadline = time.time() + len(audio) / rate + 2.0
                while time.time() < deadline and sd.get_stream().active:
                    time.sleep(0.05)
                if time.time() >= deadline:
                    log.warning("audio device did not finish playback in time; skipping")
                    sd.stop()
            except Exception:
                log.exception("playback failed")
            finally:
                self._playing.clear()
