"""Role-guard refusals at process start print clean stderr lines and exit 1 with no traceback."""

import os
import subprocess
import sys
from pathlib import Path

ROOT = str(Path(__file__).resolve().parent.parent)
SECRET = "s" * 40


def _env(**extra: str) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in ("ROLE", "PLEX_TOKEN", "PLEX_URL", "INTERNAL_CORE_SECRET")}
    env.update(PYTHONPATH=ROOT, DATA_DIR=os.path.join(ROOT, ".pytest_cache", "startup_refusal"))
    env.update(extra)
    return env


GATEWAY = dict(
    ROLE="gateway",
    INTERNAL_CORE_SECRET=SECRET,
    PLEX_TOKEN="x",
    TRACKSEERR_CORE_URL="http://core:5251",
    APPLICATION_URL="https://r.example",
)


def _run(args: list[str], env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(args, env=env, cwd=ROOT, capture_output=True, text=True, timeout=120)


def _assert_clean_refusal(proc: subprocess.CompletedProcess) -> None:
    assert proc.returncode == 1, proc.stderr
    assert "PLEX_TOKEN" in proc.stderr
    assert "Two-tier deployment" in proc.stderr
    assert "Traceback" not in proc.stderr


def test_cli_gateway_with_plex_token_refuses_cleanly():
    _assert_clean_refusal(_run([sys.executable, "-m", "trackseerr"], _env(**GATEWAY)))


def test_importing_app_module_gateway_with_plex_token_refuses_cleanly():
    proc = _run([sys.executable, "-c", "from trackseerr.api.app import app"], _env(**GATEWAY))
    _assert_clean_refusal(proc)


def test_uvicorn_style_attribute_lookup_weak_secret_refuses_cleanly():
    env = _env(ROLE="core", INTERNAL_CORE_SECRET="short")
    proc = _run([sys.executable, "-c", "import trackseerr.api.app as m; m.app"], env)
    assert proc.returncode == 1
    assert "INTERNAL_CORE_SECRET" in proc.stderr
    assert "Traceback" not in proc.stderr


def test_all_in_one_import_and_lazy_app_still_work():
    code = (
        "import trackseerr.api.app as m, trackseerr.cli;"
        "assert 'app' not in vars(m);"
        "a = m.app; assert m.app is a; print('ok')"
    )
    proc = _run([sys.executable, "-c", code], _env())
    assert proc.returncode == 0, proc.stderr
    assert "ok" in proc.stdout
