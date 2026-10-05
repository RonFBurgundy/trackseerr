"""Tiny VALID audio files, generated in Python (no binary blobs), that pass the import security gate: correct magic
bytes and a header mutagen parses. Use these instead of placeholder text wherever a test drives the worker import."""

import struct
from pathlib import Path


def flac_bytes(sample_rate: int = 44100, channels: int = 2, bits: int = 16, total_samples: int = 44100) -> bytes:
    """``fLaC`` + a single (last) STREAMINFO block; ~42 bytes. mutagen.flac reads rate/channels/bits/length from it."""
    packed = (sample_rate << 44) | ((channels - 1) << 41) | ((bits - 1) << 36) | total_samples
    streaminfo = struct.pack(">HH", 4096, 4096) + b"\x00" * 6 + struct.pack(">Q", packed) + b"\x00" * 16
    return b"fLaC" + bytes([0x80, 0, 0, 34]) + streaminfo


def mp3_bytes(frames: int = 4) -> bytes:
    """MPEG-1 Layer III, 128 kbps, 44.1 kHz, stereo: 417-byte frames; mutagen needs several to sync."""
    frame = b"\xff\xfb\x90\x00" + b"\x00" * 413
    return frame * frames


def wav_bytes(samples: int = 400) -> bytes:
    """8 kHz mono 8-bit PCM WAV, ~444 bytes."""
    data = b"\x80" * samples
    fmt = struct.pack("<HHIIHH", 1, 1, 8000, 8000, 1, 8)
    body = b"WAVE" + b"fmt " + struct.pack("<I", len(fmt)) + fmt + b"data" + struct.pack("<I", len(data)) + data
    return b"RIFF" + struct.pack("<I", len(body)) + body


def write_flac(path: Path | str, **kw) -> Path:
    p = Path(path)
    p.write_bytes(flac_bytes(**kw))
    return p


def write_mp3(path: Path | str, frames: int = 4) -> Path:
    p = Path(path)
    p.write_bytes(mp3_bytes(frames))
    return p


def write_wav(path: Path | str, samples: int = 400) -> Path:
    p = Path(path)
    p.write_bytes(wav_bytes(samples))
    return p
