"""End-to-end tests of the OAuth authorization server (device sign-in with Trakt)."""

import asyncio
import ipaddress
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from tests.conftest import (
    BASE_URL,
    CLIENT_REDIRECT,
    PASSPHRASE,
    Clock,
    McpSession,
    Tokens,
    authorize,
    consent_form,
    device_sign_in,
    login,
    pkce_pair,
    register,
    through_consent,
    to_login_page,
    unlock,
)
from tests.fake_trakt import FakeTrakt
from trakt_mcp.auth.provider import PASSPHRASE_SETTING, REFRESH_REUSE_GRACE_SECONDS
from trakt_mcp.config import Settings
from trakt_mcp.server import App
from trakt_mcp.trakt.errors import TraktUnavailableError


def redirect_query(response: httpx.Response) -> dict[str, list[str]]:
    assert response.status_code in (302, 303), response.text
    return parse_qs(urlsplit(response.headers["location"]).query)


async def refresh(client: httpx.AsyncClient, tokens: Tokens, refresh_token: str) -> httpx.Response:
    return await client.post(
        "/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": tokens.client_id,
            "client_secret": tokens.client_secret or "",
        },
    )


async def mcp_status(client: httpx.AsyncClient, access_token: str) -> int:
    response = await McpSession(client, access_token).rpc("tools/list")
    return response.status_code


def device_requests(fake: FakeTrakt, path: str) -> int:
    return sum(1 for r in fake.requests if r.url.path == path)


async def new_login_page(client: httpx.AsyncClient) -> tuple[str, str]:
    reg = (await register(client)).json()
    _, challenge = pkce_pair()
    return reg["client_id"], await to_login_page(client, reg["client_id"], challenge)


async def unlocked_login_page(client: httpx.AsyncClient) -> str:
    _, path = await new_login_page(client)
    assert (await unlock(client, path)).status_code == 303
    return path


async def consent_for_new_client(
    client: httpx.AsyncClient, fake: FakeTrakt, clock: Clock
) -> tuple[str, httpx.Response]:
    client_id, path = await new_login_page(client)
    page = await device_sign_in(client, fake, clock, path)
    assert page.status_code == 200 and "Allow access" in page.text, page.text
    return client_id, page


# -- discovery & registration ---------------------------------------------


async def test_metadata_advertises_pkce_and_registration(client: httpx.AsyncClient) -> None:
    meta = (await client.get("/.well-known/oauth-authorization-server")).json()
    assert meta["issuer"] == BASE_URL
    assert meta["registration_endpoint"] == f"{BASE_URL}/register"
    assert meta["code_challenge_methods_supported"] == ["S256"]
    resource = (await client.get("/.well-known/oauth-protected-resource/mcp")).json()
    assert resource["resource"] == f"{BASE_URL}/mcp"


async def test_registration_accepts_allowlisted_redirect(client: httpx.AsyncClient) -> None:
    response = await register(client)
    assert response.status_code == 201, response.text
    assert response.json()["redirect_uris"] == [CLIENT_REDIRECT]


async def test_registration_rejects_foreign_redirect(client: httpx.AsyncClient) -> None:
    response = await register(client, redirect="https://attacker.example/cb")
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_redirect_uri"


async def test_registration_flood_cannot_lock_out_the_owner(
    client: httpx.AsyncClient, settings: Settings, fake_trakt: FakeTrakt, clock: Clock
) -> None:
    for _ in range(settings.max_registered_clients * 2):
        assert (await register(client)).status_code == 201
    await login(client, fake_trakt, clock)


async def test_registration_keeps_approved_clients(
    app: App, client: httpx.AsyncClient, settings: Settings, tokens: Tokens
) -> None:
    for _ in range(settings.max_registered_clients * 2):
        await register(client)
    assert await app.store.get_client(tokens.client_id) is not None
    assert await app.store.count_clients(approved=False) <= settings.max_registered_clients


async def test_unapproved_clients_expire(
    app: App, client: httpx.AsyncClient, settings: Settings, clock: Clock
) -> None:
    stale = (await register(client)).json()["client_id"]
    clock.now += settings.unapproved_client_ttl_seconds + 1
    await register(client)  # triggers the purge
    assert await app.store.get_client(stale) is None


async def test_approved_clients_do_not_expire(
    app: App, client: httpx.AsyncClient, settings: Settings, tokens: Tokens, clock: Clock
) -> None:
    clock.now += settings.unapproved_client_ttl_seconds + 1
    await register(client)
    assert await app.store.get_client(tokens.client_id) is not None


# -- the happy path ---------------------------------------------------------


async def test_full_login_grants_mcp_access(client: httpx.AsyncClient, tokens: Tokens) -> None:
    assert await mcp_status(client, tokens.access_token) == 200


async def test_unauthenticated_mcp_request_gets_401_with_discovery(
    client: httpx.AsyncClient,
) -> None:
    response = await client.post("/mcp", json={})
    assert response.status_code == 401
    assert "resource_metadata=" in response.headers["www-authenticate"]


async def test_invalid_bearer_token_is_rejected(client: httpx.AsyncClient) -> None:
    assert await mcp_status(client, "not-a-token") == 401


async def test_trakt_tokens_are_stored_for_the_user(app: App, tokens: Tokens) -> None:
    assert await app.store.get_trakt_tokens("alice") is not None


# -- device sign-in -------------------------------------------------------------


async def test_login_page_shows_the_code_and_reloads(
    client: httpx.AsyncClient, fake_trakt: FakeTrakt
) -> None:
    path = await unlocked_login_page(client)
    page = await client.get(path)
    [user_code] = [d[0] for d in fake_trakt.devices.values()]
    assert user_code in page.text
    assert "https://trakt.tv/activate" in page.text
    assert "http-equiv='refresh' content='5'" in page.text
    assert page.headers["cache-control"] == "no-store"


async def test_device_code_is_requested_once_per_sign_in(
    client: httpx.AsyncClient, fake_trakt: FakeTrakt, clock: Clock
) -> None:
    path = await unlocked_login_page(client)
    for _ in range(3):
        await client.get(path)
        clock.now += 5
    assert device_requests(fake_trakt, "/oauth/device/code") == 1


async def test_trakt_is_never_polled_faster_than_the_interval(
    client: httpx.AsyncClient, fake_trakt: FakeTrakt, clock: Clock
) -> None:
    path = await unlocked_login_page(client)
    await client.get(path)
    for _ in range(5):
        await client.get(path)  # reloads within the interval
    assert device_requests(fake_trakt, "/oauth/device/token") == 0
    clock.now += 5
    await client.get(path)
    assert device_requests(fake_trakt, "/oauth/device/token") == 1


async def test_slow_down_increases_the_interval(
    client: httpx.AsyncClient, fake_trakt: FakeTrakt, clock: Clock
) -> None:
    _, path = await new_login_page(client)
    page = await device_sign_in(client, fake_trakt, clock, path, status="slow_down")
    assert "content='10'" in page.text


async def test_trakt_denial_returns_access_denied(
    client: httpx.AsyncClient, fake_trakt: FakeTrakt, clock: Clock
) -> None:
    _, path = await new_login_page(client)
    response = await device_sign_in(client, fake_trakt, clock, path, status="denied")
    query = redirect_query(response)
    assert query["error"] == ["access_denied"] and query["state"] == ["client-state"]
    assert (await client.get(path)).status_code == 400  # the sign-in is over


async def test_expired_device_code(client: httpx.AsyncClient, clock: Clock) -> None:
    path = await unlocked_login_page(client)
    await client.get(path)
    clock.now += 601
    assert (await client.get(path)).status_code == 400


async def test_login_page_for_unknown_request(client: httpx.AsyncClient) -> None:
    assert (await client.get("/login?request=nope")).status_code == 400
    assert (await client.get("/login")).status_code == 400


async def test_trakt_unreachable_when_starting(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, app: App
) -> None:
    async def broken() -> None:
        raise TraktUnavailableError("down")

    monkeypatch.setattr(app.provider._trakt_oauth, "device_code", broken)
    path = await unlocked_login_page(client)
    response = await client.get(path)
    assert response.status_code == 400 and "not reachable" in response.text


async def test_trakt_unreachable_while_polling_keeps_waiting(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, app: App, clock: Clock
) -> None:
    path = await unlocked_login_page(client)
    await client.get(path)

    async def broken(_: str) -> None:
        raise TraktUnavailableError("down")

    monkeypatch.setattr(app.provider._trakt_oauth, "poll_device_token", broken)
    clock.now += 5
    page = await client.get(path)
    assert page.status_code == 200 and "Waiting for Trakt" in page.text


async def test_trakt_user_not_on_allowlist_is_denied_and_revoked(
    app: App, client: httpx.AsyncClient, fake_trakt: FakeTrakt, clock: Clock
) -> None:
    fake_trakt.username = "mallory"
    _, path = await new_login_page(client)
    response = await device_sign_in(client, fake_trakt, clock, path)
    assert redirect_query(response)["error"] == ["access_denied"]
    assert "code" not in redirect_query(response)
    assert await app.store.get_trakt_tokens("mallory") is None
    assert fake_trakt.revoked == 1


async def test_display_name_alone_does_not_match_allowlist(
    client: httpx.AsyncClient, fake_trakt: FakeTrakt, settings: Settings, clock: Clock
) -> None:
    """Only the slug counts; the fake's display name differs from its slug."""
    settings.allowed_trakt_users = ["alice-display-name"]
    _, path = await new_login_page(client)
    response = await device_sign_in(client, fake_trakt, clock, path)
    assert redirect_query(response)["error"] == ["access_denied"]


async def test_reading_the_trakt_account_fails(
    client: httpx.AsyncClient, fake_trakt: FakeTrakt, clock: Clock
) -> None:
    path = await unlocked_login_page(client)
    await client.get(path)
    fake_trakt.approve_device()
    fake_trakt.fail_next = 503
    clock.now += 5
    response = await client.get(path)
    assert redirect_query(response)["error"] == ["server_error"]


# -- owner passphrase (device-code phishing protection) -------------------------


async def test_sign_in_asks_for_the_passphrase_before_any_trakt_code(
    client: httpx.AsyncClient, fake_trakt: FakeTrakt
) -> None:
    _, path = await new_login_page(client)
    page = await client.get(path)
    assert page.status_code == 200 and "type='password'" in page.text
    assert device_requests(fake_trakt, "/oauth/device/code") == 0  # nothing to phish with


async def test_phishing_with_an_approved_client_fails_without_the_passphrase(
    client: httpx.AsyncClient, fake_trakt: FakeTrakt, tokens: Tokens
) -> None:
    """Round-4 attack: attacker knows an approved client_id and wants a device code."""
    _, challenge = pkce_pair()
    path = await to_login_page(client, tokens.client_id, challenge)
    before = device_requests(fake_trakt, "/oauth/device/code")
    assert (await unlock(client, path, "guess")).status_code == 400
    page = await client.get(path)
    assert "type='password'" in page.text
    assert device_requests(fake_trakt, "/oauth/device/code") == before


async def test_wrong_passphrase_shows_the_form_again(client: httpx.AsyncClient) -> None:
    _, path = await new_login_page(client)
    response = await unlock(client, path, "wrong")
    assert response.status_code == 400 and "Wrong passphrase" in response.text


async def test_sign_in_is_discarded_after_five_wrong_passphrases(
    client: httpx.AsyncClient,
) -> None:
    _, path = await new_login_page(client)
    for _ in range(4):
        assert (await unlock(client, path, "wrong")).status_code == 400
    final = await unlock(client, path, "wrong")
    assert "Start connecting again" in final.text
    assert (await unlock(client, path)).status_code == 400  # even the right one is too late


async def test_per_ip_limit_on_wrong_passphrases(client: httpx.AsyncClient, clock: Clock) -> None:
    for _ in range(3):
        _, path = await new_login_page(client)
        for _ in range(4):
            await unlock(client, path, "wrong")
    _, path = await new_login_page(client)
    response = await unlock(client, path)  # correct, but this IP used up its 10 tries
    assert "try again in an hour" in response.text
    clock.now += 3601
    _, path = await new_login_page(client)
    assert (await unlock(client, path)).status_code == 303


async def test_server_wide_limit_with_many_ips(
    client: httpx.AsyncClient, settings: Settings
) -> None:
    settings.trusted_proxies = [ipaddress.ip_network("127.0.0.0/8")]
    for i in range(100):
        _, path = await new_login_page(client)
        request_id = parse_qs(urlsplit(path).query)["request"][0]
        await client.post(
            "/login",
            data={"request": request_id, "passphrase": "wrong"},
            headers={"X-Forwarded-For": f"203.0.113.{i % 250}, 10.0.0.{i % 250}"},
        )
    _, path = await new_login_page(client)
    request_id = parse_qs(urlsplit(path).query)["request"][0]
    response = await client.post(
        "/login",
        data={"request": request_id, "passphrase": PASSPHRASE},
        headers={"X-Forwarded-For": "198.51.100.1"},
    )
    assert "try again in an hour" in response.text


async def test_parallel_guesses_cannot_exceed_the_limit(
    app: App, client: httpx.AsyncClient
) -> None:
    """Round-5 finding: checks and records happen under one lock."""
    paths = [(await new_login_page(client))[1] for _ in range(30)]
    await asyncio.gather(*(unlock(client, path, "wrong") for path in paths))
    assert await app.store.count_recent_failures(3600) == 10


async def test_unlocked_sign_in_cannot_be_continued_from_another_browser(
    app: App, client: httpx.AsyncClient, fake_trakt: FakeTrakt, clock: Clock
) -> None:
    """Round-5 attack: attacker sends the owner the real link and polls along."""
    _, path = await new_login_page(client)
    assert (await unlock(client, path)).status_code == 303  # the owner's browser
    transport = httpx.ASGITransport(app=app.asgi)
    async with httpx.AsyncClient(transport=transport, base_url=BASE_URL) as attacker:
        await client.get(path)  # owner sees the code
        fake_trakt.approve_device()
        clock.now += 5
        stolen = await attacker.get(path)
    assert stolen.status_code == 400 and "another browser" in stolen.text
    assert "location" not in stolen.headers


async def test_consent_needs_the_browser_that_signed_in(
    client: httpx.AsyncClient, fake_trakt: FakeTrakt, clock: Clock
) -> None:
    _, page = await consent_for_new_client(client, fake_trakt, clock)
    client.cookies.delete("__Host-trakt-mcp-login")
    response = await client.post("/consent", data={**consent_form(page), "action": "approve"})
    assert response.status_code == 400


async def test_passphrase_page_names_the_client(client: httpx.AsyncClient) -> None:
    _, path = await new_login_page(client)
    page = await client.get(path)
    assert "Test" in page.text and "claude.ai" in page.text and "link someone sent" in page.text


async def test_unusable_stored_hash_never_unlocks(app: App, client: httpx.AsyncClient) -> None:
    await app.store.set_setting(PASSPHRASE_SETTING, "")
    _, path = await new_login_page(client)
    assert (await unlock(client, path)).status_code == 400


async def test_missing_passphrase_explains_how_to_set_it(
    app: App, client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def nothing(_: str) -> None:
        return None

    _, path = await new_login_page(client)
    monkeypatch.setattr(app.store, "get_setting", nothing)
    for response in (await client.get(path), await unlock(client, path)):
        assert response.status_code == 400 and "set-passphrase" in response.text


async def test_approved_client_still_needs_the_passphrase(
    client: httpx.AsyncClient, fake_trakt: FakeTrakt, clock: Clock, tokens: Tokens
) -> None:
    _, challenge = pkce_pair()
    path = await to_login_page(client, tokens.client_id, challenge)
    assert "type='password'" in (await client.get(path)).text
    response = await device_sign_in(client, fake_trakt, clock, path)
    assert "code" in redirect_query(response)


async def test_login_locks_do_not_accumulate(
    app: App, client: httpx.AsyncClient, fake_trakt: FakeTrakt, clock: Clock
) -> None:
    for i in range(20):
        await client.get(f"/login?request=junk{i}")
    await login(client, fake_trakt, clock)
    assert app.provider._login_locks == {}


# -- consent (confused-deputy protection) ------------------------------------


async def test_new_client_gets_a_consent_page_after_sign_in(
    client: httpx.AsyncClient, fake_trakt: FakeTrakt, clock: Clock
) -> None:
    _, page = await consent_for_new_client(client, fake_trakt, clock)
    assert "Test" in page.text and "claude.ai" in page.text and "alice" in page.text
    assert "just now" in page.text
    csp = page.headers["content-security-policy"]
    assert "frame-ancestors 'none'" in csp and "form-action 'self' https://claude.ai" in csp
    cookie = page.headers["set-cookie"]
    assert cookie.startswith("__Host-trakt-mcp-consent=")
    assert "HttpOnly" in cookie and "Secure" in cookie and "SameSite=strict" in cookie


async def test_consent_page_shows_how_old_the_client_is(
    app: App, client: httpx.AsyncClient, fake_trakt: FakeTrakt, clock: Clock
) -> None:
    reg = (await register(client)).json()
    clock.now += 50 * 60  # still within the 1 h unapproved-client TTL
    _, challenge = pkce_pair()
    path = await to_login_page(client, reg["client_id"], challenge)
    page = await device_sign_in(client, fake_trakt, clock, path)
    assert "50 minutes ago" in page.text


async def test_sign_in_for_a_foreign_client_needs_explicit_approval(
    app: App, client: httpx.AsyncClient, fake_trakt: FakeTrakt, clock: Clock
) -> None:
    client_id, page = await consent_for_new_client(client, fake_trakt, clock)
    assert "location" not in page.headers  # no code without a decision
    assert not await app.store.client_approved(client_id, "alice")


async def test_approved_client_skips_consent_next_time(
    client: httpx.AsyncClient, fake_trakt: FakeTrakt, clock: Clock, tokens: Tokens
) -> None:
    _, challenge = pkce_pair()
    path = await to_login_page(client, tokens.client_id, challenge)
    response = await device_sign_in(client, fake_trakt, clock, path)
    assert "code" in redirect_query(response)


async def test_approval_is_bound_to_the_trakt_account(
    client: httpx.AsyncClient,
    fake_trakt: FakeTrakt,
    clock: Clock,
    settings: Settings,
    tokens: Tokens,
) -> None:
    settings.allowed_trakt_users = ["alice", "bob"]
    fake_trakt.username = "bob"
    _, challenge = pkce_pair()
    path = await to_login_page(client, tokens.client_id, challenge)
    page = await device_sign_in(client, fake_trakt, clock, path)
    assert page.status_code == 200 and "bob" in page.text  # alice's approval does not count


async def test_consent_denied_returns_access_denied(
    client: httpx.AsyncClient, fake_trakt: FakeTrakt, clock: Clock
) -> None:
    _, page = await consent_for_new_client(client, fake_trakt, clock)
    response = await through_consent(client, page, action="deny")
    query = redirect_query(response)
    assert query["error"] == ["access_denied"] and query["state"] == ["client-state"]
    assert urlsplit(response.headers["location"]).hostname == "claude.ai"


async def test_consent_post_without_cookie_is_rejected(
    app: App, client: httpx.AsyncClient, fake_trakt: FakeTrakt, clock: Clock
) -> None:
    """A cross-site auto-submitted form carries no SameSite=Strict cookie."""
    client_id, page = await consent_for_new_client(client, fake_trakt, clock)
    client.cookies.clear()
    response = await client.post("/consent", data={**consent_form(page), "action": "approve"})
    assert response.status_code == 400
    assert not await app.store.client_approved(client_id, "alice")


async def test_consent_post_with_wrong_token_is_rejected(
    client: httpx.AsyncClient, fake_trakt: FakeTrakt, clock: Clock
) -> None:
    _, page = await consent_for_new_client(client, fake_trakt, clock)
    forged = {**consent_form(page), "csrf": "forged", "action": "approve"}
    client.cookies.set("__Host-trakt-mcp-consent", "forged")
    assert (await client.post("/consent", data=forged)).status_code == 400


async def test_consent_token_is_bound_to_its_request(
    client: httpx.AsyncClient, fake_trakt: FakeTrakt, clock: Clock
) -> None:
    _, first = await consent_for_new_client(client, fake_trakt, clock)
    _, second = await consent_for_new_client(client, fake_trakt, clock)
    mixed = {
        "request": consent_form(first)["request"],
        "csrf": consent_form(second)["csrf"],
        "action": "approve",
    }
    assert (await client.post("/consent", data=mixed)).status_code == 400


async def test_consent_decision_is_single_use(
    client: httpx.AsyncClient, fake_trakt: FakeTrakt, clock: Clock
) -> None:
    _, page = await consent_for_new_client(client, fake_trakt, clock)
    fields = {**consent_form(page), "action": "approve"}
    cookie = page.cookies["__Host-trakt-mcp-consent"]
    assert (await client.post("/consent", data=fields)).status_code == 302
    client.cookies.set("__Host-trakt-mcp-consent", cookie)
    assert (await client.post("/consent", data=fields)).status_code == 400


async def test_consent_post_for_unknown_request(client: httpx.AsyncClient) -> None:
    assert (await client.post("/consent", data={})).status_code == 400
    assert (await client.post("/consent", data={"request": "x", "csrf": "y"})).status_code == 400


async def test_consent_id_cannot_be_used_as_login_request(
    client: httpx.AsyncClient, fake_trakt: FakeTrakt, clock: Clock
) -> None:
    _, page = await consent_for_new_client(client, fake_trakt, clock)
    consent_id = consent_form(page)["request"]
    assert (await client.get(f"/login?request={consent_id}")).status_code == 400


# -- authorize input validation & flooding ------------------------------------


@pytest.mark.parametrize(
    "extra",
    [{"code_challenge": "short"}, {"code_challenge": "x" * 43 + "!"}, {"state": "s" * 513}],
)
async def test_authorize_rejects_malformed_input(
    client: httpx.AsyncClient, extra: dict[str, str]
) -> None:
    reg = (await register(client)).json()
    _, challenge = pkce_pair()
    response = await authorize(client, reg["client_id"], challenge, **extra)
    assert redirect_query(response)["error"] == ["invalid_request"]


async def test_authorize_rejects_foreign_resource(client: httpx.AsyncClient) -> None:
    reg = (await register(client)).json()
    _, challenge = pkce_pair()
    response = await authorize(
        client, reg["client_id"], challenge, resource="https://other.example/mcp"
    )
    assert redirect_query(response)["error"] == ["invalid_target"]


async def test_authorize_flood_cannot_lock_out_the_owner(
    client: httpx.AsyncClient, settings: Settings, fake_trakt: FakeTrakt, clock: Clock
) -> None:
    settings.max_pending_logins = 3
    junk = (await register(client)).json()
    _, challenge = pkce_pair()
    for _ in range(10):
        await authorize(client, junk["client_id"], challenge)
    await login(client, fake_trakt, clock)


async def test_flood_cannot_evict_the_owners_consent(
    client: httpx.AsyncClient, settings: Settings, fake_trakt: FakeTrakt, clock: Clock
) -> None:
    settings.max_pending_logins = 3
    settings.max_registered_clients = 2
    _, page = await consent_for_new_client(client, fake_trakt, clock)
    _, challenge = pkce_pair()
    for _ in range(10):
        junk = (await register(client)).json()
        for _ in range(6):
            await authorize(client, junk["client_id"], challenge)
    response = await through_consent(client, page)
    assert "code" in redirect_query(response)


async def test_flood_cannot_evict_a_client_with_a_sign_in_in_progress(
    app: App, client: httpx.AsyncClient, settings: Settings
) -> None:
    settings.max_registered_clients = 2
    owner, _ = await new_login_page(client)
    for _ in range(10):
        await register(client)
    assert await app.store.get_client(owner) is not None


# -- code exchange ------------------------------------------------------------


async def _code_for(
    client: httpx.AsyncClient, fake: FakeTrakt, clock: Clock
) -> tuple[dict[str, str], str, str]:
    reg = (await register(client)).json()
    verifier, challenge = pkce_pair()
    path = await to_login_page(client, reg["client_id"], challenge)
    response = await through_consent(client, await device_sign_in(client, fake, clock, path))
    return reg, redirect_query(response)["code"][0], verifier


def _exchange(reg: dict[str, str], code: str, verifier: str) -> dict[str, str]:
    return {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": CLIENT_REDIRECT,
        "client_id": reg["client_id"],
        "client_secret": reg.get("client_secret", ""),
        "code_verifier": verifier,
    }


async def test_code_requires_matching_pkce_verifier(
    client: httpx.AsyncClient, fake_trakt: FakeTrakt, clock: Clock
) -> None:
    reg, code, _ = await _code_for(client, fake_trakt, clock)
    wrong, _ = pkce_pair()
    response = await client.post("/token", data=_exchange(reg, code, wrong))
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_grant"


async def test_code_is_single_use(
    client: httpx.AsyncClient, fake_trakt: FakeTrakt, clock: Clock
) -> None:
    reg, code, verifier = await _code_for(client, fake_trakt, clock)
    assert (await client.post("/token", data=_exchange(reg, code, verifier))).status_code == 200
    replay = await client.post("/token", data=_exchange(reg, code, verifier))
    assert replay.status_code == 400


async def test_code_is_bound_to_the_client(
    client: httpx.AsyncClient, fake_trakt: FakeTrakt, clock: Clock
) -> None:
    _, code, verifier = await _code_for(client, fake_trakt, clock)
    other = (await register(client)).json()
    response = await client.post("/token", data=_exchange(other, code, verifier))
    assert response.status_code == 400


# -- refresh tokens -----------------------------------------------------------


async def test_refresh_rotates_tokens(client: httpx.AsyncClient, tokens: Tokens) -> None:
    response = await refresh(client, tokens, tokens.refresh_token)
    assert response.status_code == 200, response.text
    new = response.json()
    assert new["refresh_token"] != tokens.refresh_token
    assert await mcp_status(client, new["access_token"]) == 200


async def test_refresh_token_replay_revokes_the_family(
    client: httpx.AsyncClient, tokens: Tokens, clock: Clock
) -> None:
    new = (await refresh(client, tokens, tokens.refresh_token)).json()
    clock.now += REFRESH_REUSE_GRACE_SECONDS + 1
    replay = await refresh(client, tokens, tokens.refresh_token)
    assert replay.status_code == 400
    # The legitimate descendant tokens are revoked too: someone has a stolen token.
    assert await mcp_status(client, new["access_token"]) == 401
    assert (await refresh(client, tokens, new["refresh_token"])).status_code == 400


async def test_quick_refresh_retry_is_refused_but_keeps_the_session(
    client: httpx.AsyncClient, tokens: Tokens
) -> None:
    new = (await refresh(client, tokens, tokens.refresh_token)).json()
    retry = await refresh(client, tokens, tokens.refresh_token)
    assert retry.status_code == 400
    assert await mcp_status(client, new["access_token"]) == 200


async def test_refresh_token_is_bound_to_the_client(
    client: httpx.AsyncClient, tokens: Tokens
) -> None:
    other = (await register(client)).json()
    response = await client.post(
        "/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": tokens.refresh_token,
            "client_id": other["client_id"],
            "client_secret": other["client_secret"],
        },
    )
    assert response.status_code == 400


async def test_refresh_cannot_widen_scope(client: httpx.AsyncClient, tokens: Tokens) -> None:
    response = await client.post(
        "/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": tokens.refresh_token,
            "client_id": tokens.client_id,
            "client_secret": tokens.client_secret or "",
            "scope": "trakt admin",
        },
    )
    assert response.status_code == 400


async def test_session_has_an_absolute_lifetime(
    client: httpx.AsyncClient, settings: Settings, tokens: Tokens, clock: Clock
) -> None:
    current, elapsed, step = tokens.refresh_token, 0, 20 * 86400
    # Refresh every 20 days (well within the refresh token's 30-day TTL);
    # the session must still end after max_session_seconds.
    while elapsed < 2 * settings.max_session_seconds:
        clock.now += step
        elapsed += step
        response = await refresh(client, tokens, current)
        if response.status_code != 200:
            break
        current = response.json()["refresh_token"]
    assert settings.max_session_seconds <= elapsed < settings.max_session_seconds + step


# -- revocation & allowlist changes ------------------------------------------


async def test_revocation_kills_access_and_refresh(
    client: httpx.AsyncClient, tokens: Tokens
) -> None:
    response = await client.post(
        "/revoke",
        data={
            "token": tokens.access_token,
            "client_id": tokens.client_id,
            "client_secret": tokens.client_secret or "",
        },
    )
    assert response.status_code == 200
    assert await mcp_status(client, tokens.access_token) == 401
    assert (await refresh(client, tokens, tokens.refresh_token)).status_code == 400


async def test_removing_user_from_allowlist_takes_effect_immediately(
    client: httpx.AsyncClient, settings: Settings, tokens: Tokens
) -> None:
    settings.allowed_trakt_users = ["someone-else"]
    assert await mcp_status(client, tokens.access_token) == 401


async def test_expired_access_token_is_rejected(
    client: httpx.AsyncClient, tokens: Tokens, clock: Clock
) -> None:
    clock.now += 3601
    assert await mcp_status(client, tokens.access_token) == 401


async def test_second_login_works(
    client: httpx.AsyncClient, fake_trakt: FakeTrakt, clock: Clock
) -> None:
    first = await login(client, fake_trakt, clock)
    second = await login(client, fake_trakt, clock)
    assert first.access_token != second.access_token
    assert await mcp_status(client, first.access_token) == 200
    assert await mcp_status(client, second.access_token) == 200
