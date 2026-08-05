"""Audio device enumeration via pyaudiowpatch."""

from __future__ import annotations

import logging
from typing import Any

import pyaudiowpatch as pyaudio

from app.schemas import DeviceInfo

logger = logging.getLogger(__name__)


def list_devices() -> list[DeviceInfo]:
    devices: list[DeviceInfo] = []
    pa = pyaudio.PyAudio()
    try:
        wasapi_info = pa.get_host_api_info_by_type(pyaudio.paWASAPI)
        default_loopback = pa.get_default_wasapi_loopback()
        default_input = pa.get_default_input_device_info()

        for i in range(pa.get_device_count()):
            dev = pa.get_device_info_by_index(i)
            if dev.get("hostApi") != wasapi_info["index"]:
                continue
            max_in = int(dev.get("maxInputChannels", 0))
            if max_in <= 0:
                continue
            is_loopback = dev.get("isLoopbackDevice", False)
            kind = "loopback" if is_loopback else "input"
            devices.append(
                DeviceInfo(
                    index=i,
                    name=str(dev.get("name", f"Device {i}")),
                    kind=kind,
                    default_sample_rate=float(dev.get("defaultSampleRate", 44100)),
                    max_input_channels=max_in,
                )
            )

        for d in devices:
            if d.index == default_loopback["index"]:
                d.name = f"{d.name} (default loopback)"
            if d.index == default_input["index"]:
                d.name = f"{d.name} (default mic)"
    finally:
        pa.terminate()
    return devices


def get_device_info(index: int) -> dict[str, Any]:
    pa = pyaudio.PyAudio()
    try:
        return pa.get_device_info_by_index(index)
    finally:
        pa.terminate()
