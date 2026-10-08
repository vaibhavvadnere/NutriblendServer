"""Client IP behind Cloudflare, and production hardening switches."""

import importlib

from starlette.requests import Request


def _request(headers: dict, client=("10.0.0.5", 1234)) -> Request:
    return Request({
        "type": "http", "method": "GET", "path": "/", "query_string": b"",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()], "client": client,
    })


def test_client_ip_ignores_headers_unless_trusted(monkeypatch):
    from app.core import deps
    from app.core.config import settings

    req = _request({"CF-Connecting-IP": "1.2.3.4", "X-Forwarded-For": "5.6.7.8"})
    monkeypatch.setattr(settings, "TRUST_PROXY_HEADERS", False)
    assert deps.client_ip(req) == "10.0.0.5"
    monkeypatch.setattr(settings, "TRUST_PROXY_HEADERS", True)
    assert deps.client_ip(req) == "1.2.3.4"  # Cloudflare's header wins
    assert deps.client_ip(_request({"X-Forwarded-For": "5.6.7.8, 10.0.0.1"})) == "5.6.7.8"
    assert deps.client_ip(_request({})) == "10.0.0.5"


def test_docs_hidden_in_production(monkeypatch):
    from fastapi.testclient import TestClient

    from app.core.config import settings
    import app.main as main

    assert TestClient(main.app).get("/openapi.json").status_code == 200
    monkeypatch.setattr(settings, "ENV", "production")
    monkeypatch.setattr(settings, "CORS_ORIGINS", [])
    prod = importlib.reload(main)
    try:
        client = TestClient(prod.app)
        assert client.get("/docs").status_code == 404
        assert client.get("/openapi.json").status_code == 404
        assert "access-control-allow-origin" not in client.get(
            "/privacy-policy", headers={"Origin": "https://evil.example"}).headers
    finally:
        monkeypatch.undo()
        importlib.reload(main)
