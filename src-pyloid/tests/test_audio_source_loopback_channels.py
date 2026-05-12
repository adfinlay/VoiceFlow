"""Regression tests for the WASAPI loopback channel-count fallback.

A real Windows user reported every meeting-recording attempt produced a
44-byte WAV (header only, zero audio frames) and the log carried:

    [WARN] [meeting] preview start failed |
        {"error": "Could not open audio device 8: Error opening InputStream:
         Invalid number of channels [PaErrorCode -9998]"}

PortAudio's `paInvalidChannelCount` (-9998). WASAPI loopback / Stereo Mix
devices on Windows are stereo-only — `sd.InputStream(channels=1, ...)` is
rejected. The fix in `audio_source.start()` is to consult the device's
`max_input_channels` and, on loopback, open at the device-native channel
count first (the existing callback already downmixes to mono).

These tests fake `sounddevice` to assert the fallback iterates as expected
without touching real audio hardware.
"""

from __future__ import annotations

import sys
import types
from typing import Any

import numpy as np
import pytest


@pytest.fixture
def fake_sd(monkeypatch):
    """Install a fake `sounddevice` module under sys.modules. Returns the
    module so individual tests can configure its `InputStream` behaviour
    and inspect the resulting call log."""
    sd = types.ModuleType("sounddevice")

    sd.calls: list[dict[str, Any]] = []
    sd.fail_for_channels: set[int] = set()

    def query_devices(idx):
        # Two stub devices: id=8 is loopback (stereo only), id=21 is mono mic.
        if idx == 8:
            return {
                "name": "Speakers (Realtek) [Loopback]",
                "max_input_channels": 2,
                "default_samplerate": 48000.0,
            }
        if idx == 21:
            return {
                "name": "Microphone Array",
                "max_input_channels": 1,
                "default_samplerate": 16000.0,
            }
        return {"max_input_channels": 1, "default_samplerate": 16000.0}

    sd.query_devices = query_devices

    class WasapiSettings:
        def __init__(self, loopback: bool = False) -> None:
            self.loopback = loopback

    sd.WasapiSettings = WasapiSettings

    class _FakeStream:
        def __init__(self, **kwargs):
            sd.calls.append(kwargs)
            if kwargs.get("channels") in sd.fail_for_channels:
                raise RuntimeError(
                    "Error opening InputStream: Invalid number of channels "
                    "[PaErrorCode -9998]"
                )
            self._started = False

        def start(self):
            self._started = True

        def stop(self):
            self._started = False

        def close(self):
            pass

    sd.InputStream = _FakeStream

    monkeypatch.setitem(sys.modules, "sounddevice", sd)
    return sd


def _on_frames(_arr: np.ndarray) -> None:
    pass


class TestLoopbackChannelFallback:
    def test_loopback_opens_at_native_channel_count(self, fake_sd):
        """Loopback device should be opened at max_input_channels (2), not 1."""
        from services.recording.audio_source import SoundDeviceAudioSource

        src = SoundDeviceAudioSource(device_id=8, loopback=True)
        src.start(_on_frames)
        # First (and only successful) call should ask for stereo.
        assert fake_sd.calls[0]["channels"] == 2
        assert fake_sd.calls[0]["device"] == 8
        src.stop()

    def test_mic_keeps_mono_open(self, fake_sd):
        """Plain mic open keeps the prior channels=1 default."""
        from services.recording.audio_source import SoundDeviceAudioSource

        src = SoundDeviceAudioSource(device_id=21, loopback=False)
        src.start(_on_frames)
        assert fake_sd.calls[0]["channels"] == 1
        assert fake_sd.calls[0]["device"] == 21
        src.stop()

    def test_loopback_falls_back_to_mono_if_native_rejected(self, fake_sd):
        """If somehow a loopback device's native channel count is also rejected,
        try channels=1 before giving up (keeps the safety net wide)."""
        from services.recording.audio_source import SoundDeviceAudioSource

        # Reject every stereo open — the iteration should land on mono.
        fake_sd.fail_for_channels = {2}
        src = SoundDeviceAudioSource(device_id=8, loopback=True)
        src.start(_on_frames)
        # Last successful call had channels=1.
        opened = next(c for c in fake_sd.calls if c["channels"] == 1)
        assert opened["device"] == 8
        src.stop()

    def test_loopback_passes_wasapi_loopback_extra_settings(self, fake_sd):
        """Make sure the WasapiSettings(loopback=True) flag is still attached
        on Windows (regression guard against accidentally dropping it)."""
        from services.recording.audio_source import SoundDeviceAudioSource

        src = SoundDeviceAudioSource(device_id=8, loopback=True)
        src.start(_on_frames)
        extra = fake_sd.calls[0].get("extra_settings")
        assert extra is not None
        assert getattr(extra, "loopback", False) is True
        src.stop()

    def test_loopback_raises_when_every_attempt_fails(self, fake_sd):
        """If both stereo AND mono opens fail, raise so the caller's WARN
        message surfaces the real PortAudio error."""
        from services.recording.audio_source import SoundDeviceAudioSource

        fake_sd.fail_for_channels = {1, 2}
        src = SoundDeviceAudioSource(device_id=8, loopback=True)
        with pytest.raises(RuntimeError, match="Could not open audio device 8"):
            src.start(_on_frames)
