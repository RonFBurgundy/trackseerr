#!/usr/bin/env python3
"""Icon generation utility for TrackSeerr Unraid and Web UI branding assets.

Renders high-resolution PNG assets from master vector SVGs using local `rsvg-convert`
if available, or automatically falling back to an isolated Docker container with
`librsvg2-bin`.
"""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys


def find_repo_root() -> Path:
    """Find the root directory of the repository."""
    current = Path(__file__).resolve().parent
    if (current / "trackseerr.svg").is_file():
        # current is 'unraid', parent is repo root
        return current.parent
    return Path.cwd()


def render_svg_to_png(
    svg_path: Path | str,
    png_path: Path | str,
    width: int = 512,
    height: int = 512,
) -> bool:
    """Render an SVG file to a PNG file of the specified dimensions.

    Uses `rsvg-convert` if available in PATH, otherwise falls back to a docker container.

    Args:
        svg_path: Path to source SVG file.
        png_path: Path to output PNG file.
        width: Target width in pixels.
        height: Target height in pixels.

    Returns:
        True if rendering succeeded, False otherwise.
    """
    svg = Path(svg_path).resolve()
    png = Path(png_path).resolve()

    if not svg.is_file():
        print(f"Error: Source SVG not found at {svg}", file=sys.stderr)
        return False

    png.parent.mkdir(parents=True, exist_ok=True)

    rsvg_bin = shutil.which("rsvg-convert")
    if rsvg_bin:
        print(f"Rendering {svg.name} -> {png.name} ({width}x{height}) via local rsvg-convert...")
        cmd = [
            rsvg_bin,
            "-w",
            str(width),
            "-h",
            str(height),
            str(svg),
            "-o",
            str(png),
        ]
        try:
            subprocess.run(cmd, capture_output=True, text=True, check=True)
            print(f"Successfully generated {png} ({png.stat().st_size} bytes)")
            return True
        except subprocess.CalledProcessError as exc:
            print(f"Local rsvg-convert failed: {exc.stderr}", file=sys.stderr)
            # Fall through to Docker fallback

    docker_bin = shutil.which("docker")
    if docker_bin:
        print(f"Rendering {svg.name} -> {png.name} ({width}x{height}) via Docker fallback...")
        repo_root = find_repo_root()
        try:
            rel_svg = svg.relative_to(repo_root)
            rel_png = png.relative_to(repo_root)
        except ValueError:
            rel_svg = svg
            rel_png = png

        cmd = [
            docker_bin,
            "run",
            "--rm",
            "-v",
            f"{repo_root}:/app",
            "debian:bookworm-slim",
            "sh",
            "-c",
            (
                "apt-get update -qq && "
                "apt-get install -y -qq librsvg2-bin >/dev/null && "
                f"rsvg-convert -w {width} -h {height} /app/{rel_svg} -o /app/{rel_png} && "
                f"chown {os.getuid()}:{os.getgid()} /app/{rel_png}"
            ),
        ]
        try:
            subprocess.run(cmd, capture_output=True, text=True, check=True)
            if png.is_file():
                print(f"Successfully generated {png} ({png.stat().st_size} bytes)")
                return True
        except subprocess.CalledProcessError as exc:
            print(f"Docker fallback failed: {exc.stderr}", file=sys.stderr)
            return False

    print("Error: Neither 'rsvg-convert' nor 'docker' is available to render SVG.", file=sys.stderr)
    return False


REQUESTS_BODY_STOPS = {
    "#2d5594": "#0f766e",
    "#1e3a6d": "#115e59",
    "#152747": "#134e4a",
    "#0e1b33": "#0a2e2b",
}


def write_requests_variant(src: Path, dst: Path) -> None:
    """Write the teal "TrackSeerr Requests" variant SVG (recoloured body, same artwork)."""
    svg = src.read_text(encoding="utf-8")
    for old, new in REQUESTS_BODY_STOPS.items():
        svg = svg.replace(f'stop-color="{old}"', f'stop-color="{new}"')
    svg = svg.replace("#38bdf8", "#5eead4")
    dst.write_text(svg, encoding="utf-8")


def build_all_icons() -> bool:
    """Build all TrackSeerr branding icons."""
    repo_root = find_repo_root()
    requests_svg = repo_root / "unraid" / "trackseerr-requests.svg"
    write_requests_variant(repo_root / "unraid" / "trackseerr.svg", requests_svg)
    svg_source = repo_root / "unraid" / "trackseerr.svg"
    targets = [
        (svg_source, repo_root / "unraid" / "trackseerr.png", 512, 512),
        (svg_source, repo_root / "plex_playlist_sync" / "static" / "favicon.png", 64, 64),
        (requests_svg, repo_root / "unraid" / "trackseerr-requests.png", 512, 512),
    ]

    success = True
    for src, dst, w, h in targets:
        if not render_svg_to_png(src, dst, w, h):
            success = False
    return success


def main() -> int:
    """CLI entrypoint."""
    success = build_all_icons()
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
