"""Plex PIN forward_url must stay on the app's own origin (open-redirect guard)."""

from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from trackseerr.api.app import create_app
from trackseerr.api.dependencies import get_config, get_db
from trackseerr.config import Config
from trackseerr.local_auth import parse_trusted_proxies, resolve_request_origin
from trackseerr.security import safe_forward_url

REQ = "https://app.example.com"


def sfu(candidate, app=None, req=REQ):
    return safe_forward_url(candidate, allowed_origins=[req, app])


def test_same_origin_absolute_passes():
    assert sfu("https://app.example.com/settings?x=1") == "https://app.example.com/settings?x=1"
    assert sfu("https://app.example.com") == "https://app.example.com"


def test_relative_path_resolved():
    assert sfu("/library?tab=a") == "https://app.example.com/library?tab=a"
    assert sfu("/x", app="https://pub.example.org/ts/", req=None) == "https://pub.example.org/ts/x"


def test_union_of_application_url_and_request_origin():
    app = "https://public.example"
    req = "https://rontube.tailb0577.ts.net"
    assert sfu("https://public.example/a", app=app, req=req) == "https://public.example/a"
    assert sfu("https://rontube.tailb0577.ts.net/a", app=app, req=req) == "https://rontube.tailb0577.ts.net/a"
    assert sfu("https://evil.com/a", app=app, req=req) is None
    assert sfu("/x", app=app, req=req) == "https://rontube.tailb0577.ts.net/x"


def test_request_origin_used_when_no_application_url():
    assert sfu("http://other.local/a", req="http://other.local") == "http://other.local/a"
    assert sfu("https://app.example.com/a", req=None) is None


@pytest.mark.parametrize(
    "bad",
    [
        "https://evil.com/",
        "//evil.com",
        "/\\evil.com",
        "\\\\evil.com",
        "https://app@evil.com",
        "https://app.example.com@evil.com/",
        "https://user:pw@app.example.com/",
        "javascript:alert(1)",
        "data:text/html,x",
        "http://app.example.com/",  # scheme mismatch
        "https://app.example.com:8443/",  # port mismatch
        "https://app.example.com.evil.com/",
        "https://app.example.com/\nx",
        "https://app.example.com/ x",
        "https://app.example.com/a\tb",
        "ftp://app.example.com/",
        "",
        "   ",
        None,
    ],
)
def test_rejects(bad):
    assert sfu(bad) is None


def test_normalization_case_trailing_dot_default_port():
    assert sfu("https://APP.Example.COM./a") == "https://APP.Example.COM./a"
    assert sfu("https://app.example.com:443/a") == "https://app.example.com:443/a"
    assert sfu("http://x.test:80/a", req="http://x.test") == "http://x.test:80/a"
    assert sfu("https://app.example.com/a", req="https://APP.example.com.:443") == "https://app.example.com/a"


def test_idn_compared_by_punycode():
    assert sfu("https://bücher.example/a", req="https://xn--bcher-kva.example") == "https://bücher.example/a"
    assert sfu("https://bücher.example/a", req="https://evil.example") is None


def test_rejection_log_omits_path_and_query(caplog):
    with caplog.at_level("WARNING"):
        sfu("https://evil.com/steal?token=SECRETVALUE")
    assert "evil.com" in caplog.text
    assert "SECRETVALUE" not in caplog.text


# --- request origin / trusted proxies

def test_forwarded_headers_ignored_from_untrusted_peer():
    trusted = parse_trusted_proxies("10.0.0.0/8")
    o = resolve_request_origin("http", "internal:8000", "203.0.113.9", "https", "evil.com", trusted)
    assert o == "http://internal:8000"


def test_forwarded_headers_ignored_when_no_trusted_proxies():
    assert resolve_request_origin("http", "h:1", "10.0.0.2", "https", "evil.com", ()) == "http://h:1"


def test_forwarded_headers_honored_from_trusted_peer():
    trusted = parse_trusted_proxies("10.0.0.0/8")
    o = resolve_request_origin("http", "internal:8000", "10.1.2.3", "https", "app.example.com", trusted)
    assert o == "https://app.example.com"


def test_forwarded_host_garbage_rejected_from_trusted_peer():
    trusted = parse_trusted_proxies("10.0.0.0/8")
    o = resolve_request_origin("http", "internal:8000", "10.1.2.3", "https", "a.com/@evil", trusted)
    assert o == "https://internal:8000"


# --- endpoint

@pytest.fixture
def make_client(tmp_path):
    from trackseerr.storage import Database

    db = Database(":memory:")

    def _make(app_url=None, trusted=None, client_addr=None, **kw):
        cfg = Config(
            plex_url="http://127.0.0.1:32400",
            plex_token="t",
            data_dir=str(tmp_path),
            spotify_client_id="a",
            spotify_client_secret="b",
            application_url=app_url,
            trusted_proxies=trusted,
        )
        app = create_app(db=db, config=cfg)
        app.dependency_overrides[get_db] = lambda: db
        app.dependency_overrides[get_config] = lambda: cfg
        return TestClient(app, client=client_addr, **kw) if client_addr else TestClient(app, **kw)

    return _make


def _post_pin(client, forward_url, headers=None):
    pin = {"id": 77, "code": "ABCD"}
    with patch("trackseerr.auth.requests.post") as post:
        post.return_value.json.return_value = pin
        post.return_value.raise_for_status.return_value = None
        resp = client.post("/api/auth/plex/pin", json={"forward_url": forward_url}, headers=headers or {})
    assert resp.status_code == 200
    q = parse_qs(urlsplit(resp.json()["auth_url"]).fragment.split("?", 1)[1])
    return q


def test_endpoint_good_forward_url_includes_pin_id(make_client):
    q = _post_pin(make_client(), "http://testserver/settings")
    fwd = q["forwardUrl"][0]
    assert fwd.startswith("http://testserver/settings")
    assert parse_qs(urlsplit(fwd).query)["pin_id"] == ["77"]


def test_endpoint_bad_forward_url_omitted(make_client):
    for bad in ("https://evil.com/", "//evil.com", "https://testserver@evil.com/", "javascript:alert(1)"):
        assert "forwardUrl" not in _post_pin(make_client(), bad)


def test_endpoint_application_url_and_request_host_both_allowed(make_client):
    c = make_client(app_url="https://public.example", base_url="https://rontube.tailb0577.ts.net")
    assert _post_pin(c, "https://public.example/x")["forwardUrl"][0].startswith("https://public.example/x")
    assert _post_pin(c, "https://rontube.tailb0577.ts.net/x")["forwardUrl"][0].startswith(
        "https://rontube.tailb0577.ts.net/x"
    )
    assert "forwardUrl" not in _post_pin(c, "https://evil.com/x")


def test_endpoint_spoofed_forwarded_host_ignored_from_untrusted(make_client):
    c = make_client(trusted="10.0.0.0/8", client_addr=("203.0.113.9", 5555))
    q = _post_pin(c, "https://evil.com/", headers={"X-Forwarded-Host": "evil.com", "X-Forwarded-Proto": "https"})
    assert "forwardUrl" not in q


def test_endpoint_forwarded_host_honored_from_trusted_proxy(make_client):
    c = make_client(trusted="10.0.0.0/8", client_addr=("10.1.2.3", 5555))
    q = _post_pin(
        c, "https://app.example.com/x", headers={"X-Forwarded-Host": "app.example.com", "X-Forwarded-Proto": "https"}
    )
    assert q["forwardUrl"][0].startswith("https://app.example.com/x")
