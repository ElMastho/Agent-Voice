"""Where the sound comes out: headphones (only you hear it) or speakers (everyone around does).

Windows only: the default playback device's form factor, as the audio settings know it (pycaw). The device id is
read every few seconds (~1 ms); its properties only when it changes (plugging headphones in, a Bluetooth headset
connecting).
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
    """The default playback device, and what kind it is."""

    def __init__(self):
        self._id = None
        self.kind = ""
        self.name = ""

    def poll(self) -> str:
        if sys.platform != "win32":
            return ""
        import comtypes
        from pycaw.api.mmdeviceapi import IMMDeviceEnumerator
        from pycaw.constants import CLSID_MMDeviceEnumerator, EDataFlow, ERole
        from pycaw.pycaw import AudioUtilities

        comtypes.CoInitialize()
        enum = comtypes.CoCreateInstance(CLSID_MMDeviceEnumerator, IMMDeviceEnumerator, comtypes.CLSCTX_INPROC_SERVER)
        dev = enum.GetDefaultAudioEndpoint(EDataFlow.eRender.value, ERole.eMultimedia.value)
        device_id = dev.GetId()
        if device_id != self._id:
            self._id = device_id
            info = AudioUtilities.CreateDevice(dev)
            self.kind, self.name = kind_of(info.properties.get(FORM_FACTOR)), info.FriendlyName or ""
            log.info("sound output: %s (%s)", self.name, self.kind or "unknown kind")
        return self.kind
