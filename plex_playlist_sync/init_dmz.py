"""``python -m plex_playlist_sync init-dmz``: scaffold a two-container (gateway + core) deployment.

Writes ``docker-compose.dmz.yml`` and ``.env`` (mode 0600) and prints the Unraid values. It makes no
network calls, never overwrites an existing file (it writes ``<name>.new`` instead), and keeps every
secret out of stdout except the freshly generated ``INTERNAL_CORE_SECRET``, shown once. Secrets from an
existing all-in-one container are carried by reference (``${NAME}`` in compose, value only in ``.env``).
See ``docs/dmz-ergonomics.md`` section 5.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import secrets
import sys
from pathlib import Path
from typing import IO, Mapping, Optional
from urllib.parse import urlsplit

from plex_playlist_sync.role_guard import GATEWAY_FORBIDDEN_ENV

COMPOSE_NAME = "docker-compose.dmz.yml"
ENV_NAME = ".env"
IMAGE = "ghcr.io/ronfburgundy/trackseerr:latest"
DEFAULT_NETWORK = "trackseerr-internal"
DEFAULT_PROXY_NETWORK = "proxynet"
DEFAULT_PUBLIC_URL = "https://requests.example.com"
CORE_PORT = 5251
GATEWAY_PORT = 5250
PROC_ENVIRON = "/proc/self/environ"

# Carried into core as references: the value lives only in .env.
SECRET_ENV: tuple[str, ...] = tuple(GATEWAY_FORBIDDEN_ENV)

# Carried into core verbatim (non-secret settings).
CARRY_ENV: tuple[str, ...] = (
    "PLEX_URL",
    "PLEX_VERIFY_SSL",
    "IGNORE_SSL",
    "PLEX_MUSIC_SECTION",
    "PLEX_MACHINE_IDENTIFIER",
    "SECONDS_TO_WAIT",
    "SEARCH_SIMILARITY_THRESHOLD",
    "LOG_LEVEL",
    "TZ",
    "PUID",
    "PGID",
    "UMASK",
    "SPOTIFY_USER_ID",
    "SPOTIFY_PLAYLIST_ID",
    "SPOTIFY_PLAYLIST_IDS",
    "DEEZER_USER_ID",
    "DEEZER_PLAYLIST_ID",
    "DEEZER_PLAYLIST_IDS",
    "LIDARR_URL",
    "LIDARR_AUTO_SEARCH",
    "LIDARR_ROOT_FOLDER",
    "LIDARR_QUALITY_PROFILE_ID",
    "LIDARR_METADATA_PROFILE_ID",
    "LIDARR_TRICKLE_RATE_SECONDS",
    "LIDARR_TRICKLE_BATCH_SIZE",
    "LIDARR_AUTO_TRICKLE",
    "LIDARR_AUTO_TRICKLE_INTERVAL_MINUTES",
    "AUTO_APPROVE_REQUESTS",
    "ENABLE_BACKLOG_SEARCH",
    "BACKLOG_SEARCH_INTERVAL_MINUTES",
    "ENABLE_RSS_SYNC",
    "RSS_SYNC_INTERVAL_MINUTES",
    "WRITE_MISSING_AS_CSV",
    "APPEND_SERVICE_SUFFIX",
    "ADD_PLAYLIST_POSTER",
    "ADD_PLAYLIST_DESCRIPTION",
    "APPEND_INSTEAD_OF_SYNC",
    "MUSICBRAINZ_URL",
    "MUSICBRAINZ_MIRROR_URL",
)

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def validate_bind(value: str) -> Optional[str]:
    """Returns the normalised bind IP, or None when it is not a specific, valid IPv4/IPv6 address."""
    try:
        addr = ipaddress.ip_address(value.strip())
    except ValueError:
        return None
    if addr.is_unspecified:
        return None
    mapped = getattr(addr, "ipv4_mapped", None)
    if mapped is not None and mapped.is_unspecified:
        return None
    return str(addr)


def bind_for_compose(addr: str) -> str:
    """IPv6 must be bracketed in a compose ``host:port:port`` mapping."""
    return f"[{addr}]" if ":" in addr else addr


def valid_public_url(url: str) -> bool:
    if not url or any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in url):
        return False
    try:
        parts = urlsplit(url)
        parts.port  # noqa: B018  (raises ValueError on a bad port)
    except ValueError:
        return False
    return parts.scheme in ("http", "https") and bool(parts.hostname)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m plex_playlist_sync init-dmz",
        description=(
            "Scaffold a two-container TrackSeerr deployment (public gateway + internal core). "
            "Writes docker-compose.dmz.yml and .env (mode 0600) and prints the Unraid values. "
            "Never overwrites an existing file (writes <name>.new instead). Makes no network calls."
        ),
    )
    parser.add_argument(
        "--from-existing",
        action="store_true",
        help="carry the settings of a running all-in-one container into the core service "
        "(read from /proc/self/environ, or --env-file); secrets go to .env by reference only",
    )
    parser.add_argument("--env-file", metavar="PATH", help="read the existing settings from this env file instead")
    parser.add_argument("--public-url", metavar="URL", help="public https URL of the request portal (APPLICATION_URL)")
    parser.add_argument(
        "--core-lan-bind", metavar="IP", default="127.0.0.1",
        help="host IP the core admin port is published on (default 127.0.0.1; use your LAN IP, never 0.0.0.0)",
    )
    parser.add_argument(
        "--network", metavar="NAME", default=DEFAULT_NETWORK,
        help=f"name of the internal gateway<->core Docker network (default {DEFAULT_NETWORK})",
    )
    parser.add_argument("--out", metavar="DIR", default=".", help="output directory (default: current directory)")
    return parser


# --------------------------------------------------------------------------- reading existing settings


def parse_env_text(text: str) -> dict[str, str]:
    """Parses ``KEY=VALUE`` lines (comments, ``export`` and simple quotes tolerated)."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if _ENV_KEY_RE.match(key):
            out[key] = value
    return out


def read_proc_environ(path: str = PROC_ENVIRON) -> dict[str, str]:
    raw = Path(path).read_bytes()
    out: dict[str, str] = {}
    for chunk in raw.split(b"\0"):
        if b"=" not in chunk:
            continue
        key, _, value = chunk.partition(b"=")
        out[key.decode("utf-8", "replace")] = value.decode("utf-8", "replace")
    return out


# --------------------------------------------------------------------------- rendering


def _env_value(value: str) -> str:
    """Quotes a value for a compose ``.env`` file (single quotes keep ``$`` literal)."""
    if "'" not in value and "\n" not in value and "\r" not in value:
        return f"'{value}'"
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$").replace("\n", "\\n")
    return f'"{escaped}"'


def _yaml_scalar(value: str) -> str:
    """A YAML double-quoted scalar with ``$`` doubled so compose does not interpolate it."""
    return json.dumps(value.replace("$", "$$"))


def render_env(secret: str, public_url: str, bind: str, network: str, carried_secrets: Mapping[str, str]) -> str:
    lines = [
        "# Generated by `python -m plex_playlist_sync init-dmz`. Contains secrets: keep this file mode 0600.",
        f"INTERNAL_CORE_SECRET={_env_value(secret)}",
        f"APPLICATION_URL={_env_value(public_url)}",
        f"CORE_LAN_BIND={_env_value(bind_for_compose(bind))}",
        f"TRACKSEERR_INTERNAL_NETWORK={_env_value(network)}",
        f"PROXY_NETWORK={_env_value(DEFAULT_PROXY_NETWORK)}",
    ]
    for name in sorted(carried_secrets):
        lines.append(f"{name}={_env_value(carried_secrets[name])}")
    return "\n".join(lines) + "\n"


def render_compose(carried_plain: Mapping[str, str], carried_secret_names: list[str]) -> str:
    # Mapping form: in compose's list form (``- KEY="v"``) the quotes would become part of the value.
    core_env = [
        '      ROLE: "core"',
        '      INTERNAL_CORE_SECRET: "${INTERNAL_CORE_SECRET:?set by init-dmz in .env}"',
        '      APPLICATION_URL: "${APPLICATION_URL:?set by init-dmz in .env}"',
        '      CORE_LAN_BIND: "${CORE_LAN_BIND:-127.0.0.1}"',
        f'      PORT: "{CORE_PORT}"',
    ]
    for name in carried_secret_names:
        core_env.append(f'      {name}: "${{{name}}}"')
    for name in sorted(carried_plain):
        core_env.append(f"      {name}: {_yaml_scalar(carried_plain[name])}")
    env_block = "\n".join(core_env)
    return f"""# Generated by `python -m plex_playlist_sync init-dmz`. Safe to commit: it holds no secret values.
# Secrets live in .env (mode 0600) and are referenced as ${{NAME}}.
services:
  trackseerr-requests:
    # Public request portal (gateway). No volumes, no Plex/Last.fm/download credentials.
    image: {IMAGE}
    container_name: trackseerr-requests
    restart: unless-stopped
    ports:
      - "{GATEWAY_PORT}:{GATEWAY_PORT}"
    environment:
      - ROLE=gateway
      - INTERNAL_CORE_SECRET=${{INTERNAL_CORE_SECRET:?set by init-dmz in .env}}
      - TRACKSEERR_CORE_URL=http://trackseerr-core:{CORE_PORT}
      - APPLICATION_URL=${{APPLICATION_URL:?set by init-dmz in .env}}
      - PORT={GATEWAY_PORT}
      # - TRUSTED_PROXIES=172.18.0.0/16
    networks:
      - proxynet
      - internal

  trackseerr-core:
    # Admin engine (core): LAN/loopback only, holds all state and secrets.
    image: {IMAGE}
    container_name: trackseerr-core
    restart: unless-stopped
    ports:
      - "${{CORE_LAN_BIND:-127.0.0.1}}:{CORE_PORT}:{CORE_PORT}"
    volumes:
      - ./appdata:/config
      - /path/to/data:/data
    environment:
{env_block}
    networks:
      - internal
      - core-lan

networks:
  # Your EXISTING reverse-proxy / cloudflared network. cloudflared must share it with the gateway, never core.
  proxynet:
    name: ${{PROXY_NETWORK:-{DEFAULT_PROXY_NETWORK}}}
    external: true
  # Private gateway<->core link: no egress, no published ports.
  internal:
    name: ${{TRACKSEERR_INTERNAL_NETWORK:-{DEFAULT_NETWORK}}}
    internal: true
  core-lan:
    name: trackseerr-core-lan
    driver: bridge
"""


# --------------------------------------------------------------------------- writing (never overwrite)


def write_new_file(directory: Path, name: str, content: str, mode: int) -> Path:
    """Creates ``name`` exclusively; if it exists, uses ``name.new`` (then ``.new.1`` ...). Never overwrites."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    candidates = [name, f"{name}.new"] + [f"{name}.new.{i}" for i in range(1, 100)]
    for candidate in candidates:
        path = directory / candidate
        try:
            fd = os.open(path, flags, mode)
        except FileExistsError:
            continue
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(content)
        except OSError:
            path.unlink(missing_ok=True)
            raise
        os.chmod(path, mode)  # umask may have narrowed it, never widened; make the intent explicit
        return path
    raise FileExistsError(f"too many existing {name}.new files in {directory}")


# --------------------------------------------------------------------------- command


def main(
    argv: Optional[list[str]] = None,
    *,
    stdout: Optional[IO[str]] = None,
    stderr: Optional[IO[str]] = None,
    proc_environ_path: str = PROC_ENVIRON,
) -> int:
    out = stdout if stdout is not None else sys.stdout
    err = stderr if stderr is not None else sys.stderr
    args = build_parser().parse_args(argv)

    if not _NAME_RE.match(args.network):
        print(f"ERROR: invalid --network name: {args.network!r}", file=err)
        return 2
    bind = validate_bind(args.core_lan_bind)
    if bind is None:
        print("ERROR: --core-lan-bind must be a specific IPv4/IPv6 LAN or loopback address, never 0.0.0.0 or ::", file=err)
        return 2
    public_url = (args.public_url or "").strip().rstrip("/")
    if public_url and not valid_public_url(public_url):
        print("ERROR: --public-url must be an http:// or https:// URL with a host and no whitespace", file=err)
        return 2
    if not public_url:
        public_url = DEFAULT_PUBLIC_URL
        print(f"NOTE: no --public-url given; wrote the placeholder {DEFAULT_PUBLIC_URL}. Edit APPLICATION_URL in .env.", file=err)

    existing: dict[str, str] = {}
    if args.from_existing or args.env_file:
        try:
            if args.env_file:
                existing = parse_env_text(Path(args.env_file).read_text(encoding="utf-8"))
            else:
                existing = read_proc_environ(proc_environ_path)
        except OSError as exc:
            print(f"ERROR: could not read the existing settings: {exc.strerror or type(exc).__name__}", file=err)
            return 2

    carried_secrets = {n: existing[n] for n in SECRET_ENV if existing.get(n, "").strip()}
    carried_plain = {n: existing[n] for n in CARRY_ENV if existing.get(n, "").strip()}

    out_dir = Path(args.out)
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        secret = secrets.token_hex(32)
        env_text = render_env(secret, public_url, bind, args.network, carried_secrets)
        compose_text = render_compose(carried_plain, sorted(carried_secrets))
        env_path = write_new_file(out_dir, ENV_NAME, env_text, 0o600)  # first: no .env, no compose file
        compose_path = write_new_file(out_dir, COMPOSE_NAME, compose_text, 0o644)
    except OSError as exc:
        print(f"ERROR: could not write files in {out_dir}: {exc.strerror or type(exc).__name__}", file=err)
        return 1

    for wanted, actual in ((COMPOSE_NAME, compose_path), (ENV_NAME, env_path)):
        if actual.name != wanted:
            print(f"NOTICE: {wanted} already exists and was NOT overwritten; wrote {actual.name} instead.", file=out)

    print(
        f"""
TrackSeerr two-container setup
==============================
Wrote {compose_path} and {env_path} (mode 0600).
{"Carried over " + str(len(carried_plain)) + " settings; " + str(len(carried_secrets)) + " secrets went to .env by reference only." if existing else "Fresh setup: add your Plex and download settings to the core service."}

Shown ONCE (not logged anywhere else): INTERNAL_CORE_SECRET
  {secret}

Unraid values (both templates)
  INTERNAL_CORE_SECRET      the secret above (same on both)
  Internal network          {args.network}   (docker network create {args.network})
  Extra Parameters          --network={args.network}

TrackSeerr Requests (gateway)
  ROLE                      gateway
  TRACKSEERR_CORE_URL       http://trackseerr-core:{CORE_PORT}
  APPLICATION_URL           {public_url}

TrackSeerr Core
  ROLE                      core
  APPLICATION_URL           {public_url}
  Port                      {CORE_PORT} (publish on {bind} only)

Next: docker compose -f {compose_path.name} --env-file {env_path.name} up -d
See the README "Two-tier deployment" section.""",
        file=out,
    )
    return 0
