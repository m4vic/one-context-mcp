"""HTTP auth + CORS behavior of the bare ASGI app (no uvicorn needed)."""

import asyncio

from ctx.server import app


def run_app(path, method="GET", headers=None):
    """Drive the ASGI app directly and capture sent messages."""
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http",
        "path": path,
        "method": method,
        "headers": headers or [],
        "query_string": b"",
    }
    asyncio.run(app(scope, receive, send))
    return sent


def _status(sent):
    return next(m["status"] for m in sent if m["type"] == "http.response.start")


def _headers(sent):
    start = next(m for m in sent if m["type"] == "http.response.start")
    return {bytes(k): bytes(v) for k, v in start["headers"]}


# --- no token configured (default local setup) ------------------------------

def test_health_open_with_tools_when_no_auth(monkeypatch):
    monkeypatch.delenv("CTX_AUTH_TOKEN", raising=False)
    sent = run_app("/health")
    assert _status(sent) == 200
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    assert b"ctx_get" in body


def test_unknown_route_404_when_no_auth(monkeypatch):
    monkeypatch.delenv("CTX_AUTH_TOKEN", raising=False)
    assert _status(run_app("/nope")) == 404


def test_cors_wildcard_when_no_auth(monkeypatch):
    monkeypatch.delenv("CTX_AUTH_TOKEN", raising=False)
    headers = _headers(run_app("/health"))
    assert headers[b"access-control-allow-origin"] == b"*"


# --- token configured --------------------------------------------------------

def test_requests_rejected_without_token(monkeypatch):
    monkeypatch.setenv("CTX_AUTH_TOKEN", "sekret")
    assert _status(run_app("/nope")) == 401
    assert _status(run_app("/messages/abc", method="POST")) == 401


def test_wrong_token_rejected(monkeypatch):
    monkeypatch.setenv("CTX_AUTH_TOKEN", "sekret")
    sent = run_app("/nope", headers=[(b"authorization", b"Bearer wrong")])
    assert _status(sent) == 401


def test_correct_token_passes_auth(monkeypatch):
    monkeypatch.setenv("CTX_AUTH_TOKEN", "sekret")
    # /nope with valid token reaches routing and 404s (proves auth passed)
    sent = run_app("/nope", headers=[(b"authorization", b"Bearer sekret")])
    assert _status(sent) == 404


def test_health_stays_open_but_terse_with_auth(monkeypatch):
    monkeypatch.setenv("CTX_AUTH_TOKEN", "sekret")
    sent = run_app("/health")
    assert _status(sent) == 200
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    assert b"ctx_get" not in body  # tool list hidden


def test_preflight_open_with_auth(monkeypatch):
    monkeypatch.setenv("CTX_AUTH_TOKEN", "sekret")
    sent = run_app("/sse", method="OPTIONS",
                   headers=[(b"origin", b"http://localhost:3000")])
    assert _status(sent) == 204


def test_cors_reflects_only_localhost_with_auth(monkeypatch):
    monkeypatch.setenv("CTX_AUTH_TOKEN", "sekret")
    sent = run_app("/health", headers=[(b"origin", b"http://localhost:3000")])
    assert _headers(sent)[b"access-control-allow-origin"] == b"http://localhost:3000"

    sent = run_app("/health", headers=[(b"origin", b"http://evil.example.com")])
    assert b"access-control-allow-origin" not in _headers(sent)

    # localhost prefix trick must not pass: http://localhost.evil.com
    sent = run_app("/health", headers=[(b"origin", b"http://localhost.evil.com")])
    assert b"access-control-allow-origin" not in _headers(sent)
