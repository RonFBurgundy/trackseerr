"""Every route response model (and nested model) must subclass ApiModel so strict mode checks it."""

import importlib
import inspect
import pkgutil
import typing

import pydantic
from fastapi import APIRouter
from fastapi.routing import APIRoute

import plex_playlist_sync.api.routes as routes_pkg

from plex_playlist_sync.api.app import create_app
from plex_playlist_sync.api.response_models import ApiModel


def _walk(t, seen, bad, where):
    for a in typing.get_args(t):
        _walk(a, seen, bad, where)
    if inspect.isclass(t) and issubclass(t, pydantic.BaseModel) and t not in seen:
        seen.add(t)
        if not issubclass(t, ApiModel):
            bad.add(f"{t.__module__}.{t.__name__} ({where})")
        for f in t.model_fields.values():
            _walk(f.annotation, seen, bad, where)


def _app(tmp_path):
    from plex_playlist_sync.config import Config
    from plex_playlist_sync.storage import Database

    cfg = Config(plex_url="http://127.0.0.1:32400", plex_token="t", data_dir=str(tmp_path))
    return create_app(db=Database(":memory:"), config=cfg)


def _api_routes(app):
    """Every APIRoute: the app's own plus those of every APIRouter instance in each route module.

    ``app.routes`` holds included routers lazily (opaque in this FastAPI), so the module routers are
    walked directly; this also covers modules exposing several routers (quality_catalog).
    """
    out = [(f"app:{r.path}", r) for r in app.routes if isinstance(r, APIRoute)]
    for m in pkgutil.iter_modules(routes_pkg.__path__):
        mod = importlib.import_module(f"plex_playlist_sync.api.routes.{m.name}")
        for attr, obj in vars(mod).items():
            if isinstance(obj, APIRouter) and obj.__module__ is not None:
                for r in obj.routes:
                    if isinstance(r, APIRoute) and r.endpoint.__module__ == mod.__name__:
                        out.append((f"{m.name}.{attr}:{r.path}", r))
    return out


def test_all_response_models_subclass_api_model(tmp_path):
    app = _app(tmp_path)
    bad: set[str] = set()
    for where, r in _api_routes(app):
        if r.response_model is not None:
            _walk(r.response_model, set(), bad, where)
    assert not bad, sorted(bad)


def test_guard_sees_quality_catalog_routers(tmp_path):
    names = {w.split(":")[0] for w, _ in _api_routes(_app(tmp_path))}
    assert {
        "quality_catalog.definitions_router",
        "quality_catalog.formats_router",
        "quality_catalog.release_profiles_router",
    } <= names


# Routes that legitimately have no JSON response model: static/PWA files served by the app itself,
# plus streaming, file, redirect and 204 routes declared in route modules (matched by "<module>:<path>").
_NO_MODEL_ALLOWLIST: set[str] = {
    "app:/manifest.json",
    "app:/favicon.svg",
    "app:/favicon.png",
    "app:/apple-touch-icon.png",
    "app:/icon-192.png",
    "app:/icon-512.png",
    "app:/trackseerr-logo.svg",
    "app:/placeholder.svg",
    "app:/",
    "app:/invite/{token}",
    "missing:/csv",  # file download
    "missing:/rss",  # XML feed
    "missing:/text",  # plain text
    "scrobbles:/lastfm/callback",  # redirect
    "sync:/stream",  # SSE stream
    "system:/logs/download",  # file download
    "system:/logs/files/{name}",  # file download
    "system:/logs/stream",  # SSE stream
    "library:/artists/{artist_id}/image",  # file or redirect
    "library:/artists/{artist_id}/banner",  # file or redirect
    "library:/albums/{album_id}/cover",  # file or redirect
    "backups:/{name}/download",  # file download
    "lidarr_compat:/indexer/test",  # Lidarr wire contract, body varies by status
}


def test_every_json_route_has_response_model(tmp_path):
    app = _app(tmp_path)
    missing = set()
    for where, r in _api_routes(app):
        if (r.response_model is not None and r.response_model is not typing.Any) or r.status_code == 204:
            continue
        key = f"{where.split('.')[0].split(':')[0]}:{r.path}"
        if key not in _NO_MODEL_ALLOWLIST:
            missing.add(key)
    assert not missing, sorted(missing)

