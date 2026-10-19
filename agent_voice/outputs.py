"""Where the sound comes out: headphones (only you hear it) or speakers (everyone around does).

Windows: the default playback device's form factor, as the audio settings know it (pycaw); its id is read every few
seconds (~1 ms), its properties only when it changes (plugging headphones in, a Bluetooth headset connecting).
Linux: the default output's form factor or active port, from PulseAudio / PipeWire (`pactl`).
"""

from __future__ import annotations

import logging
import sys

log = logging.getLogger(__name__)

FORM_FACTOR = "{1DA5D803-D492-4EDD-8C23-E0C0FFEE7F0E} 0"  # PKEY_AudioEndpoint_FormFactor
HEADPHONES = {3, 5, 6}  # headphones, a headset, a handset
SPEAKERS = {1, 9}  # speakers, a monitor or a TV (HDMI, DisplayPort)


def kind_of(form_factor) -> str:
    """'headphones', 'speakers', or '' when the device doesn't say (line out, S/PDIF: could be either)."""
    if form_factor in HEADPHONES:
        return "headphones"
    if form_factor in SPEAKERS:
        return "speakers"
    return ""


class OutputWatch:
    """The playback device (the one picked in Settings, else the default), and what kind it is."""

    def __init__(self):
        self._id = None
        self.kind = ""
        self.name = ""
        self.preferred = ""  # Settings' speaker, by name: "" for the system's default

    def poll(self) -> str:
        if sys.platform.startswith("linux"):
            from agent_voice import pulse

            if not pulse.available():
                return ""
            kind, name = pulse.output_kind()
            if (kind, name) != (self.kind, self.name):
                self.kind, self.name = kind, name
                log.info("sound output: %s (%s)", name, kind or "unknown kind")
            return self.kind
        if sys.platform != "win32":
            return ""
        import comtypes
        from pycaw.api.mmdeviceapi import IMMDeviceEnumerator
        from pycaw.constants import CLSID_MMDeviceEnumerator, EDataFlow, ERole
        from pycaw.pycaw import AudioUtilities

        comtypes.CoInitialize()
        enum = comtypes.CoCreateInstance(CLSID_MMDeviceEnumerator, IMMDeviceEnumerator, comtypes.CLSCTX_INPROC_SERVER)
        dev = self._preferred(enum, AudioUtilities) or enum.GetDefaultAudioEndpoint(EDataFlow.eRender.value,
                                                                                   ERole.eMultimedia.value)
        device_id = dev.GetId()
        if device_id != self._id:
            self._id = device_id
            info = AudioUtilities.CreateDevice(dev)
            self.kind, self.name = kind_of(info.properties.get(FORM_FACTOR)), info.FriendlyName or ""
            log.info("sound output: %s (%s)", self.name, self.kind or "unknown kind")
        return self.kind

    def _preferred(self, enum, utilities):
        """Settings' speaker if it's plugged in (its id is checked first: reading every device's name is slower)."""
        if not self.preferred:
            return None
        if self._id and self.name == self.preferred:
            try:
                dev = enum.GetDevice(self._id)
                if dev.GetState() == 1:  # DEVICE_STATE_ACTIVE
                    return dev
            except Exception:
                pass
        found = enum.EnumAudioEndpoints(0, 1)  # playback, active
        for i in range(found.GetCount()):
            dev = found.Item(i)
            if utilities.CreateDevice(dev).FriendlyName == self.preferred:
                return dev
        return None
