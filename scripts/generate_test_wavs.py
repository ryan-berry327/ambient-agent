#!/usr/bin/env python3
"""Generate two short test WAV files for replay mode development."""

from __future__ import annotations

import math
import struct
import subprocess
import sys
import wave
from pathlib import Path

SAMPLE_RATE = 16000
OUT_DIR = Path(__file__).resolve().parent.parent / "samples"

MIC_SCRIPT = (
    "Hi, thanks for joining. I'd like to build a simple dashboard app "
    "that shows our sales numbers in real time."
)
SYSTEM_SCRIPT = (
    "Sounds good. Let's confirm we need a login page, a chart for revenue, "
    "and export to CSV. We can skip mobile for now."
)


def write_pcm_wav(path: Path, pcm: bytes, sample_rate: int = SAMPLE_RATE) -> None:
    with wave.open(str(path), "w") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm)


def generate_with_powershell_tts(text: str, path: Path) -> bool:
    """Use Windows SAPI via PowerShell — no extra Python deps."""
    ps_script = f"""
Add-Type -AssemblyName System.Speech
$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
$synth.SetOutputToWaveFile('{path.as_posix()}')
$synth.Speak('{text.replace("'", "''")}')
$synth.Dispose()
"""
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_script],
            check=True,
            capture_output=True,
            text=True,
        )
        return path.exists() and path.stat().st_size > 1000
    except Exception as exc:
        print(f"  TTS failed: {exc}", file=sys.stderr)
        return False


def resample_to_16k_mono(src: Path, dest: Path) -> None:
    """Convert any WAV to 16kHz mono using wave module (simple decimation if needed)."""
    with wave.open(str(src), "rb") as wf:
        rate = wf.getframerate()
        channels = wf.getnchannels()
        frames = wf.readframes(wf.getnframes())

    import array

    samples = array.array("h")
    samples.frombytes(frames)
    if channels > 1:
        samples = array.array("h", (samples[i] for i in range(0, len(samples), channels)))

    if rate != SAMPLE_RATE:
        ratio = rate / SAMPLE_RATE
        resampled = array.array("h")
        i = 0.0
        while int(i) < len(samples):
            resampled.append(samples[int(i)])
            i += ratio
        samples = resampled

    write_pcm_wav(dest, samples.tobytes(), SAMPLE_RATE)


def write_tone_fallback(path: Path, label: str) -> None:
    """Fallback: modulated tone (Deepgram may not transcribe meaningfully)."""
    segments = [(0.2, 200), (0.3, 0), (0.25, 180), (0.2, 0), (0.3, 220)]
    frames = bytearray()
    t = 0.0
    for duration, freq in segments:
        n = int(SAMPLE_RATE * duration)
        for i in range(n):
            if freq == 0:
                sample = 0
            else:
                mod = 0.5 + 0.5 * math.sin(2 * math.pi * 5 * (t + i / SAMPLE_RATE))
                sample = int(0.25 * 32767 * mod * math.sin(2 * math.pi * freq * (t + i / SAMPLE_RATE)))
            frames += struct.pack("<h", sample)
        t += duration
    write_pcm_wav(path, bytes(frames))
    print(f"  Wrote tone fallback for {label} (replace with real speech for Deepgram tests)")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    mic_path = OUT_DIR / "mic.wav"
    system_path = OUT_DIR / "system.wav"
    mic_tmp = OUT_DIR / "_mic_tmp.wav"
    system_tmp = OUT_DIR / "_system_tmp.wav"

    print("Generating test WAV files with Windows TTS…")
    mic_ok = generate_with_powershell_tts(MIC_SCRIPT, mic_tmp)
    sys_ok = generate_with_powershell_tts(SYSTEM_SCRIPT, system_tmp)

    if mic_ok:
        resample_to_16k_mono(mic_tmp, mic_path)
        mic_tmp.unlink(missing_ok=True)
        print(f"  mic.wav    — local speaker script")
    else:
        write_tone_fallback(mic_path, "mic")

    if sys_ok:
        resample_to_16k_mono(system_tmp, system_path)
        system_tmp.unlink(missing_ok=True)
        print(f"  system.wav — remote/call script")
    else:
        write_tone_fallback(system_path, "system")

    print(f"\nReady: {OUT_DIR}")
    print("Use replay mode (default) to push both through the pipeline.")


if __name__ == "__main__":
    main()
