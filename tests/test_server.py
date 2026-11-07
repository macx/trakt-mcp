import asyncio
import io
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.conftest import BASE_URL, Tokens
from trakt_mcp import __main__ as entrypoint
from trakt_mcp.auth.passphrase import verify_passphrase
from trakt_mcp.auth.provider import PASSPHRASE_SETTING
from trakt_mcp.server import App
from trakt_mcp.storage import Store


async def test_healthz(client: httpx.AsyncClient) -> None:
    response = await client.get("/healthz")
    assert response.json() == {"status": "ok"}


async def test_security_headers(client: httpx.AsyncClient) -> None:
    headers = (await client.get("/healthz")).headers
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["x-frame-options"] == "DENY"
    assert headers["referrer-policy"] == "no-referrer"
    assert "default-src 'none'" in headers["content-security-policy"]
    assert headers["strict-transport-security"] == "max-age=31536000"


async def test_foreign_host_header_is_rejected(app: App, tokens: Tokens) -> None:
    """DNS-rebinding protection: only the configured public host is served."""
    transport = httpx.ASGITransport(app=app.asgi)
    async with httpx.AsyncClient(transport=transport, base_url="https://evil.example") as other:
        response = await other.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={
                "Authorization": f"Bearer {tokens.access_token}",
                "Accept": "application/json, text/event-stream",
            },
        )
    assert response.status_code in (400, 403, 421)


async def test_foreign_origin_is_rejected(client: httpx.AsyncClient, tokens: Tokens) -> None:
    response = await client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers={
            "Authorization": f"Bearer {tokens.access_token}",
            "Accept": "application/json, text/event-stream",
            "Origin": "https://evil.example",
        },
    )
    assert response.status_code in (400, 403)


def test_main_reports_config_errors_without_values(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)  # no stray .env
    for var in ("TRAKT_CLIENT_ID", "ALLOWED_TRAKT_USERS", "PUBLIC_URL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("TRAKT_CLIENT_SECRET", "very-secret-value")
    with pytest.raises(SystemExit) as exit_info:
        entrypoint.main([])
    message = str(exit_info.value.code)
    assert "TRAKT_CLIENT_ID" in message and "PUBLIC_URL" in message
    assert "very-secret-value" not in message


def test_main_starts_uvicorn(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TRAKT_CLIENT_ID", "cid")
    monkeypatch.setenv("ALLOWED_TRAKT_USERS", "alice")
    monkeypatch.setenv("PUBLIC_URL", BASE_URL)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(entrypoint.uvicorn, "run", lambda app, **kw: calls.append(kw))
    entrypoint.main([])
    assert calls[0]["port"] == 8000
    assert calls[0]["proxy_headers"] is False
    assert calls[0]["server_header"] is False
    assert calls[0]["access_log"] is False


def test_set_passphrase_command(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TRAKT_CLIENT_ID", "cid")
    monkeypatch.setenv("ALLOWED_TRAKT_USERS", "alice")
    monkeypatch.setenv("PUBLIC_URL", BASE_URL)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))

    def stored() -> str:
        store = Store(tmp_path / "trakt-mcp.sqlite3")
        store.open()
        try:
            return asyncio.run(store.get_setting(PASSPHRASE_SETTING)) or ""
        finally:
            store.close()

    monkeypatch.setattr("sys.stdin", io.StringIO("a long enough passphrase\n"))
    entrypoint.main(["set-passphrase", "--stdin"])
    assert "saved" in capsys.readouterr().out
    assert "a long enough" not in stored()
    assert verify_passphrase("a long enough passphrase", stored())

    monkeypatch.setattr("sys.stdin", io.StringIO("short\n"))
    with pytest.raises(SystemExit, match="at least 12"):
        entrypoint.main(["set-passphrase", "--stdin"])

    answers = iter(["one long passphrase", "another one entirely"])
    monkeypatch.setattr(entrypoint.getpass, "getpass", lambda _: next(answers))
    with pytest.raises(SystemExit, match="do not match"):
        entrypoint.main(["set-passphrase"])

    answers = iter(["typed twice passphrase"] * 2)
    entrypoint.main(["set-passphrase"])
    assert verify_passphrase("typed twice passphrase", stored())
