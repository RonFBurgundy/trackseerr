"""Generates the tiny tagged music library the Navidrome contract tests scan (see docs/INTEGRATION_TESTS.md).

Nothing audio-related is committed: each file is a few dozen silent MPEG-1 Layer III frames (about one second,
~16 KB) with ID3v2 tags written by mutagen. ``generate`` is idempotent and returns the tracks it wrote.
"""

from dataclasses import dataclass
from pathlib import Path

from mutagen.id3 import ID3, TALB, TIT2, TPE1, TPE2, TRCK

# 128 kbps, 44.1 kHz, stereo, no padding, no CRC: header FF FB 90 00, 417 bytes per frame, 1152 samples (26.1 ms).
_FRAME = bytes([0xFF, 0xFB, 0x90, 0x00]) + bytes(413)
FRAMES_PER_FILE = 40

MUSIC_DIR = Path(__file__).resolve().parent / "music"


@dataclass(frozen=True)
class GeneratedTrack:
    artist: str
    album: str
    number: int
    title: str


TRACKS: tuple[GeneratedTrack, ...] = (
    GeneratedTrack("Tessellate Orchard", "Paper Lanterns", 1, "Kettle Song"),
    GeneratedTrack("Tessellate Orchard", "Paper Lanterns", 2, "Marmalade Sky"),
    GeneratedTrack("Tessellate Orchard", "Paper Lanterns", 3, "Quiet Engines"),
    GeneratedTrack("Velvet Kiln", "Slow Weather", 1, "Copper Rain"),
    GeneratedTrack("Velvet Kiln", "Slow Weather", 2, "Harbour Lights (2011 Remaster)"),
    GeneratedTrack("Velvet Kiln", "Slow Weather", 3, "Orange Static"),
)


def _write(path: Path, track: GeneratedTrack) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_FRAME * FRAMES_PER_FILE)
    tags = ID3()
    tags.add(TIT2(encoding=3, text=track.title))
    tags.add(TPE1(encoding=3, text=track.artist))
    tags.add(TPE2(encoding=3, text=track.artist))
    tags.add(TALB(encoding=3, text=track.album))
    tags.add(TRCK(encoding=3, text=str(track.number)))
    tags.save(path, v2_version=3)


def generate(root: Path = MUSIC_DIR) -> tuple[GeneratedTrack, ...]:
    for track in TRACKS:
        name = f"{track.number:02d} - {track.title.replace('(', '').replace(')', '')}.mp3"
        target = root / track.artist / track.album / name
        if not target.exists():
            _write(target, track)
    return TRACKS


if __name__ == "__main__":
    written = generate()
    print(f"{len(written)} tracks in {MUSIC_DIR}")
