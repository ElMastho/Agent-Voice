"""Render the README images from the real widget code.

    python scripts/render_docs.py          # writes docs/images/*.png|gif

Frames are drawn offscreen with a controlled clock, then composited over a blurred backdrop
to mimic the Windows acrylic glass. Needs Pillow (dev only).
"""

from __future__ import annotations

import math
import os
import random
import sys
from pathlib import Path

os.environ.setdefault("QT_SCALE_FACTOR", "2")  # crisp 2x renders

import numpy as np  # noqa: E402
from PIL import Image, ImageDraw, ImageFilter, ImageFont  # noqa: E402
from PySide6.QtGui import QImage  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
OUT = ROOT / "docs" / "images"

from agent_voice import widget as W  # noqa: E402

S = 2  # scale factor (matches QT_SCALE_FACTOR)


class Clock:
    """Stands in for the `time` module inside the widget: animation time is driven frame by frame."""

    now = 0.0

    def time(self) -> float:
        return self.now

    @staticmethod
    def sleep(seconds: float) -> None:
        import time

        time.sleep(seconds)


CLOCK = Clock()
W.time = CLOCK  # the widget animates from time.time(); drive it frame by frame


def backdrop(w: int, h: int, seed: int = 7) -> Image.Image:
    """A soft, colourful abstract wallpaper so the glass has something to blur."""
    rnd = random.Random(seed)
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    img = np.zeros((h, w, 3), np.float32)
    img[:] = (14, 16, 26)
    palette = [(124, 92, 255), (34, 211, 238), (244, 114, 182), (52, 211, 153), (251, 191, 36), (99, 102, 241)]
    for _ in range(9):
        cx, cy = rnd.uniform(0, w), rnd.uniform(0, h)
        r = rnd.uniform(0.18, 0.42) * max(w, h)
        col = np.array(rnd.choice(palette), np.float32)
        a = np.exp(-(((xx - cx) ** 2 + (yy - cy) ** 2) / (2 * r * r))) * rnd.uniform(0.35, 0.7)
        img = img * (1 - a[..., None]) + col * a[..., None]
    noise = np.random.default_rng(seed).normal(0, 2.0, img.shape)
    return Image.fromarray(np.clip(img + noise, 0, 255).astype(np.uint8), "RGB")


def to_pil(q: QImage) -> Image.Image:
    q = q.convertToFormat(QImage.Format_RGBA8888)
    return Image.frombuffer("RGBA", (q.width(), q.height()), bytes(q.constBits()), "raw", "RGBA", 0, 1).copy()


def rounded_mask(size, radius) -> Image.Image:
    m = Image.new("L", size, 0)
    ImageDraw.Draw(m).rounded_rectangle((0, 0, size[0] - 1, size[1] - 1), radius=radius, fill=255)
    return m


def glass_composite(bg: Image.Image, widget_img: Image.Image, x: int, y: int) -> Image.Image:
    """Shadow + blurred, tinted backdrop (acrylic) + the widget's own translucent painting."""
    out = bg.convert("RGBA")
    w, h = widget_img.size
    radius = W.RADIUS * S
    shadow = Image.new("RGBA", out.size, (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle((x, y + 10, x + w, y + h + 10), radius=radius + 6, fill=(0, 0, 0, 120))
    out = Image.alpha_composite(out, shadow.filter(ImageFilter.GaussianBlur(18)))
    under = bg.crop((x - 40, y - 40, x + w + 40, y + h + 40)).filter(ImageFilter.GaussianBlur(26))
    under = under.crop((40, 40, 40 + w, 40 + h)).convert("RGBA")
    under = Image.alpha_composite(under, Image.new("RGBA", (w, h), (18, 24, 32, 102)))  # acrylic tint
    panel = Image.alpha_composite(under, widget_img)
    out.paste(panel, (x, y), rounded_mask((w, h), radius))
    return out


def make_widget(st: dict | None, expanded: bool = False) -> W.VoiceWidget:
    w = W.VoiceWidget()
    w.poller.status.disconnect()
    w.glass = True
    w.expanded = expanded
    w.resize(W.WIDTH, W.EXPANDED_H if expanded else W.BAR_H)
    w._on_status(st)
    return w


def settle(w: W.VoiceWidget, seconds: float = 1.2) -> None:
    for _ in range(int(seconds / 0.033)):
        CLOCK.now += 0.033
        w._tick()
    w.title_t, w.flash = 1.0, 0.0


def status(phase: str, **kw) -> dict:
    base = {"phase": phase, "level": 0.0, "speaking_lang": "", "hands_free": False, "muted": phase == "muted",
            "last_heard": "", "last_heard_lang": ""}
    base.update(kw)
    return base


def frame(w: W.VoiceWidget) -> Image.Image:
    return to_pil(w.grab().toImage())


# -- images -------------------------------------------------------------------------------
def render_states() -> None:
    states = [
        ("Ready", status("idle")),
        ("Speaking", status("speaking", speaking_lang="fr")),
        ("Listening", status("waiting", hands_free=True)),
        ("Hearing you", status("recording", level=0.05)),
        ("Transcribing", status("transcribing")),
        ("Muted", status("muted")),
    ]
    cols, gap = 2, 36 * S
    shots = []
    for _, st in states:
        w = make_widget(st)
        settle(w)
        shots.append(frame(w))
    sw, sh = shots[0].size
    rows = math.ceil(len(shots) / cols)
    W_, H_ = cols * sw + (cols + 1) * gap, rows * sh + (rows + 1) * gap
    img = backdrop(W_, H_, seed=11)
    for i, shot in enumerate(shots):
        r, c = divmod(i, cols)
        img = glass_composite(img.convert("RGB"), shot, gap + c * (sw + gap), gap + r * (sh + gap))
    img.convert("RGB").save(OUT / "states.jpg", quality=90, optimize=True)


def render_panel() -> None:
    st = status("idle", hands_free=True, last_heard="Can you add tests for the server?", last_heard_lang="en")
    w = make_widget(st, expanded=True)
    settle(w)
    w.hover = "reply"
    w.hover_t["reply"] = 1.0
    shot = frame(w)
    pad = 56 * S
    img = backdrop(shot.width + 2 * pad, shot.height + 2 * pad, seed=3)
    glass_composite(img, shot, pad, pad).convert("RGB").save(OUT / "panel.jpg", quality=92, optimize=True)


def render_hero() -> None:
    w_, h_ = 1280 * S // 2 * 2, 420 * S // 2 * 2
    img = backdrop(w_, h_, seed=5)
    wd = make_widget(status("speaking", speaking_lang="en"))
    settle(wd, 2.0)
    shot = frame(wd)
    img = glass_composite(img, shot, w_ - shot.width - 150, (h_ - shot.height) // 2)
    draw = ImageDraw.Draw(img)
    try:
        title = ImageFont.truetype("seguisb.ttf", 150)
        sub = ImageFont.truetype("segoeui.ttf", 58)
        small = ImageFont.truetype("segoeui.ttf", 38)
    except OSError:
        title = sub = small = ImageFont.load_default()
    x = 150
    draw.text((x, 230), "agent-voice", font=title, fill=(255, 255, 255, 255))
    draw.text((x, 430), "Your AI coding agents, out loud.", font=sub, fill=(241, 245, 249, 245))
    draw.text((x, 520), "Local  ·  CPU-only  ·  French & English  ·  hands-free", font=small,
              fill=(226, 232, 240, 215))
    img.convert("RGB").save(OUT / "hero.jpg", quality=90, optimize=True)


def render_gif() -> None:
    """A short loop of a full conversation turn."""
    script = [
        ("idle", 1.0, {}),
        ("speaking", 3.2, {"speaking_lang": "en"}),
        ("waiting", 1.4, {}),
        ("recording", 2.6, {}),
        ("transcribing", 1.3, {}),
        ("idle", 1.4, {"last_heard": "Yes, go ahead and deploy.", "last_heard_lang": "en"}),
    ]
    w = make_widget(status("idle"))
    settle(w)
    sample = to_pil(w.grab().toImage())
    pad = 28 * S
    bg = backdrop(sample.width + 2 * pad, sample.height + 2 * pad, seed=21)
    frames, dt = [], 0.07
    rng = random.Random(1)
    for phase, seconds, extra in script:
        n = int(seconds / dt)
        for i in range(n):
            level = 0.0
            if phase == "recording":  # a speech-like envelope
                level = max(0.0, 0.045 * abs(math.sin(i * 0.45)) + rng.uniform(-0.01, 0.02))
            w._on_status(status(phase, level=level, **extra))
            for _ in range(2):  # finer animation steps per GIF frame
                CLOCK.now += dt / 2
                w._tick()
            frames.append(glass_composite(bg, frame(w), pad, pad).convert("RGB"))
    small = [f.resize((f.width // 2, f.height // 2), Image.LANCZOS) for f in frames]
    # One shared palette keeps the file small and stops colours flickering between frames.
    palette = small[len(small) // 3].quantize(colors=192, method=Image.Quantize.MEDIANCUT)
    pal = [f.quantize(palette=palette, dither=Image.Dither.NONE) for f in small]
    pal[0].save(OUT / "demo.gif", save_all=True, append_images=pal[1:], duration=int(dt * 1000), loop=0,
                optimize=True, disposal=2)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    QApplication(sys.argv)
    render_hero()
    render_states()
    render_panel()
    render_gif()
    for f in sorted(OUT.iterdir()):
        print(f"{f.name}: {f.stat().st_size // 1024} KB")


if __name__ == "__main__":
    main()
