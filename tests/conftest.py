"""Shared fixtures: a fully wired app talking to an in-memory fake Trakt."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import re
import secrets
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from tests.fake_trakt import CLIENT_ID, FakeTrakt
from trakt_mcp.auth.passphrase import hash_passphrase
from trakt_mcp.auth.provider import PASSPHRASE_SETTING
from trakt_mcp.config import Settings
from trakt_mcp.server import App, build_app

BASE_URL = "https://mcp.example.test"
PASSPHRASE = "correct horse battery staple"
PASSPHRASE_HASH = hash_passphrase(PASSPHRASE)  # computed once: scrypt is slow on purpose
CLIENT_REDIRECT = "https://claude.ai/api/mcp/auth_callback"


def make_settings(tmp_path: Path, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "trakt_client_id": CLIENT_ID,
        "allowed_trakt_users": "alice",
        "public_url": BASE_URL,
        "data_dir": tmp_path,
        **overrides,
    }
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


class Clock:
    """A controllable clock for the store (token expiry, polling intervals)."""

    def __init__(self) -> None:
        self.now = time.time()

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def fake_trakt() -> FakeTrakt:
    return FakeTrakt()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return make_settings(tmp_path)


@pytest.fixture
async def app(settings: Settings, fake_trakt: FakeTrakt, clock: Clock) -> AsyncIterator[App]:
    application = build_app(
        settings, http=httpx.AsyncClient(transport=fake_trakt.transport()), clock=clock
    )
    # The lifespan owns an anyio task group, which must be entered and exited in
    # the same task; pytest-asyncio runs fixture setup and teardown in different
    # tasks, so the lifespan lives in its own task.
    started, stop = asyncio.Event(), asyncio.Event()

    async def run_lifespan() -> None:
        async with application.asgi.router.lifespan_context(application.asgi):
            started.set()
            await stop.wait()

    task = asyncio.create_task(run_lifespan())
    await started.wait()
    await application.store.set_setting(PASSPHRASE_SETTING, PASSPHRASE_HASH)
    yield application
    stop.set()
    await task


@pytest.fixture
async def client(app: App) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app.asgi)
    async with httpx.AsyncClient(transport=transport, base_url=BASE_URL) as http:
        yield http


@dataclass
class Tokens:
    client_id: str
    client_secret: str | None
    access_token: str
    refresh_token: str


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
    return verifier, challenge.rstrip(b"=").decode()


async def register(client: httpx.AsyncClient, redirect: str = CLIENT_REDIRECT) -> httpx.Response:
    return await client.post(
        "/register",
        json={
            "redirect_uris": [redirect],
            "client_name": "Test",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "client_secret_post",
        },
    )


async def authorize(
    client: httpx.AsyncClient, client_id: str, challenge: str, **extra: str
) -> httpx.Response:
    """Call our /authorize endpoint like an MCP client would."""
    return await client.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": CLIENT_REDIRECT,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": "client-state",
            "scope": "trakt",
            **extra,
        },
    )


def consent_form(page: httpx.Response) -> dict[str, str]:
    """Extract the hidden fields of the consent form."""
    fields = dict(re.findall(r"name='(request|csrf)' value='([^']*)'", page.text))
    assert set(fields) == {"request", "csrf"}, page.text
    return fields


async def through_consent(
    client: httpx.AsyncClient, response: httpx.Response, *, action: str = "approve"
) -> httpx.Response:
    """If ``response`` is the consent page, submit ``action``; return the redirect."""
    if response.status_code != 200 or "consent" not in response.text:
        return response
    return await client.post("/consent", data={**consent_form(response), "action": action})


async def to_login_page(
    client: httpx.AsyncClient, client_id: str, challenge: str, **extra: str
) -> str:
    """Authorize and return the path of our sign-in page."""
    response = await authorize(client, client_id, challenge, **extra)
    assert response.status_code == 302, response.text
    location = urlsplit(response.headers["location"])
    assert location.path == "/login", response.headers["location"]
    return f"/login?{location.query}"


async def unlock(
    client: httpx.AsyncClient, login_path: str, passphrase: str = PASSPHRASE
) -> httpx.Response:
    """Submit the passphrase form of a sign-in page."""
    request_id = parse_qs(urlsplit(login_path).query)["request"][0]
    return await client.post("/login", data={"request": request_id, "passphrase": passphrase})


async def device_sign_in(
    client: httpx.AsyncClient,
    fake: FakeTrakt,
    clock: Clock,
    login_path: str,
    *,
    status: str = "approved",
) -> httpx.Response:
    """Unlock with the passphrase, "enter the code on Trakt", wait one interval, reload."""
    unlocked = await unlock(client, login_path)
    assert unlocked.status_code == 303, unlocked.text
    page = await client.get(login_path)
    assert page.status_code == 200 and "Open Trakt" in page.text, page.text
    fake.approve_device(status)
    clock.now += 5
    return await client.get(login_path)


async def login(client: httpx.AsyncClient, fake: FakeTrakt, clock: Clock) -> Tokens:
    """Run the complete OAuth flow (device sign-in, consent) and return our tokens."""
    reg = (await register(client)).json()
    verifier, challenge = pkce_pair()
    path = await to_login_page(client, reg["client_id"], challenge, resource=f"{BASE_URL}/mcp")
    callback = await through_consent(client, await device_sign_in(client, fake, clock, path))
    assert callback.status_code == 302, callback.text
    back = parse_qs(urlsplit(callback.headers["location"]).query)
    assert back["state"] == ["client-state"], back
    token = await client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": back["code"][0],
            "redirect_uri": CLIENT_REDIRECT,
            "client_id": reg["client_id"],
            "client_secret": reg.get("client_secret", ""),
            "code_verifier": verifier,
            "resource": f"{BASE_URL}/mcp",
        },
    )
    assert token.status_code == 200, token.text
    body = token.json()
    return Tokens(
        reg["client_id"], reg.get("client_secret"), body["access_token"], body["refresh_token"]
    )


class McpSession:
    """Minimal JSON-RPC client for the stateless streamable-HTTP endpoint."""

    def __init__(self, client: httpx.AsyncClient, access_token: str) -> None:
        self._client = client
        self._token = access_token
        self._id = 0

    async def rpc(self, method: str, params: dict[str, Any] | None = None) -> httpx.Response:
        self._id += 1
        return await self._client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": self._id, "method": method, "params": params or {}},
            headers={
                "Authorization": f"Bearer {self._token}",
                "Accept": "application/json, text/event-stream",
                "MCP-Protocol-Version": "2025-06-18",
            },
        )

    async def initialize(self) -> None:
        response = await self.rpc(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "tests", "version": "0"},
            },
        )
        assert response.status_code == 200, response.text

    async def call(self, tool: str, **arguments: Any) -> dict[str, Any]:
        response = await self.rpc("tools/call", {"name": tool, "arguments": arguments})
        assert response.status_code == 200, response.text
        return response.json()["result"]  # type: ignore[no-any-return]


@pytest.fixture
async def tokens(client: httpx.AsyncClient, fake_trakt: FakeTrakt, clock: Clock) -> Tokens:
    return await login(client, fake_trakt, clock)


@pytest.fixture
async def mcp(client: httpx.AsyncClient, tokens: Tokens) -> McpSession:
    session = McpSession(client, tokens.access_token)
    await session.initialize()
    return session
