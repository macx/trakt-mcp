import json

import httpx
import pytest

from trakt_mcp.trakt.errors import (
    TraktAuthError,
    TraktError,
    TraktForbiddenError,
    TraktLimitError,
    TraktNotFoundError,
    TraktRateLimitError,
    TraktUnavailableError,
)
from trakt_mcp.trakt.http import DevicePollStatus, TokenResponse, TraktApi, TraktOAuth


def oauth(handler: httpx.MockTransport, secret: str | None = None) -> TraktOAuth:
    return TraktOAuth(
        httpx.AsyncClient(transport=handler),
        auth_url="https://auth.trakt.tv/",
        client_id="cid",
        client_secret=secret,
        redirect_uri="https://m1.example.com/trakt/callback",
    )


def api(status: int, body: object = None, headers: dict[str, str] | None = None) -> TraktApi:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=body, headers=headers)

    return TraktApi(
        httpx.AsyncClient(transport=httpx.MockTransport(handle)),
        api_url="https://api.trakt.tv",
        client_id="cid",
    )


TOKEN_JSON = {
    "access_token": "a",
    "refresh_token": "r",
    "expires_in": 86400,
    "created_at": 100,
    "token_type": "bearer",
    "scope": "public",
}


DEVICE_JSON = {
    "device_code": "dc",
    "user_code": "ABCD1234",
    "verification_url": "https://trakt.tv/activate",
    "expires_in": 600,
    "interval": 5,
}


async def test_device_code() -> None:
    sent: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json=DEVICE_JSON)

    device = await oauth(httpx.MockTransport(handle)).device_code()
    assert (device.user_code, device.interval, device.expires_in) == ("ABCD1234", 5, 600)
    assert str(sent[0].url) == "https://auth.trakt.tv/oauth/device/code"
    assert json.loads(sent[0].content) == {"client_id": "cid"}


@pytest.mark.parametrize(("status", "error"), [(503, TraktUnavailableError), (403, TraktError)])
async def test_device_code_errors(status: int, error: type[Exception]) -> None:
    with pytest.raises(error):
        await oauth(httpx.MockTransport(lambda r: httpx.Response(status))).device_code()


async def test_device_code_malformed_and_network_error() -> None:
    with pytest.raises(TraktError, match="unexpected"):
        await oauth(httpx.MockTransport(lambda r: httpx.Response(200, json={}))).device_code()

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    with pytest.raises(TraktUnavailableError):
        await oauth(httpx.MockTransport(boom)).device_code()


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (400, DevicePollStatus.PENDING),
        (404, DevicePollStatus.EXPIRED),
        (409, DevicePollStatus.EXPIRED),
        (410, DevicePollStatus.EXPIRED),
        (418, DevicePollStatus.DENIED),
        (429, DevicePollStatus.SLOW_DOWN),
    ],
)
async def test_device_poll_statuses(status: int, expected: DevicePollStatus) -> None:
    transport = httpx.MockTransport(lambda r: httpx.Response(status))
    assert await oauth(transport).poll_device_token("dc") is expected


async def test_device_poll_success_sends_secret_if_configured() -> None:
    sent: list[dict[str, str]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(200, json=TOKEN_JSON)

    result = await oauth(httpx.MockTransport(handle), secret="s").poll_device_token("dc")
    assert isinstance(result, TokenResponse) and result.expires_at == 86500
    assert sent == [{"code": "dc", "client_id": "cid", "client_secret": "s"}]


async def test_device_poll_unexpected_status_and_network_error() -> None:
    with pytest.raises(TraktUnavailableError):
        await oauth(httpx.MockTransport(lambda r: httpx.Response(500))).poll_device_token("dc")

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    with pytest.raises(TraktUnavailableError):
        await oauth(httpx.MockTransport(boom)).poll_device_token("dc")


async def test_refresh_includes_secret_when_configured() -> None:
    sent: list[dict[str, str]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(200, json=TOKEN_JSON)

    await oauth(httpx.MockTransport(handle), secret="s3cret").refresh("r0")
    assert sent[0]["client_secret"] == "s3cret"
    assert sent[0]["grant_type"] == "refresh_token"


@pytest.mark.parametrize(
    ("status", "error"),
    [(400, TraktAuthError), (401, TraktAuthError), (503, TraktUnavailableError)],
)
async def test_token_errors(status: int, error: type[Exception]) -> None:
    transport = httpx.MockTransport(lambda r: httpx.Response(status, json={"error": "x"}))
    with pytest.raises(error):
        await oauth(transport).refresh("r")


async def test_token_network_error() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    with pytest.raises(TraktUnavailableError):
        await oauth(httpx.MockTransport(boom)).refresh("r")


async def test_malformed_token_response() -> None:
    transport = httpx.MockTransport(lambda r: httpx.Response(200, json={"access_token": "a"}))
    with pytest.raises(TraktError, match="unexpected"):
        await oauth(transport).refresh("r")


async def test_api_sends_required_headers() -> None:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True})

    client = TraktApi(
        httpx.AsyncClient(transport=httpx.MockTransport(handle)),
        api_url="https://api.trakt.tv/",
        client_id="cid",
    )
    assert await client.request("GET", "/users/settings", access_token="tok") == {"ok": True}
    headers = seen[0].headers
    assert headers["trakt-api-version"] == "2"
    assert headers["trakt-api-key"] == "cid"
    assert headers["authorization"] == "Bearer tok"
    assert str(seen[0].url) == "https://api.trakt.tv/users/settings"


async def test_api_rejects_relative_paths() -> None:
    with pytest.raises(ValueError, match="start with"):
        await api(200).request("GET", "users", access_token="t")


async def test_api_empty_response() -> None:
    assert await api(204).request("DELETE", "/x", access_token="t") is None


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (401, TraktAuthError),
        (403, TraktForbiddenError),
        (404, TraktNotFoundError),
        (420, TraktLimitError),
        (429, TraktRateLimitError),
        (502, TraktUnavailableError),
        (422, TraktError),
    ],
)
async def test_api_status_mapping(status: int, error: type[TraktError]) -> None:
    with pytest.raises(error) as info:
        await api(status).request("GET", "/x", access_token="t")
    assert info.value.status == status


async def test_rate_limit_retry_after() -> None:
    with pytest.raises(TraktRateLimitError) as info:
        await api(429, headers={"Retry-After": "12"}).request("GET", "/x", access_token="t")
    assert info.value.retry_after == 12


async def test_api_network_error() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow")

    client = TraktApi(
        httpx.AsyncClient(transport=httpx.MockTransport(boom)),
        api_url="https://api.trakt.tv",
        client_id="cid",
    )
    with pytest.raises(TraktUnavailableError):
        await client.request("GET", "/x", access_token="t")


async def test_revoke_is_best_effort() -> None:
    sent: list[dict[str, str]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        raise httpx.ConnectError("down")

    await oauth(httpx.MockTransport(handle), secret="s").revoke("tok")  # must not raise
    assert sent == [{"token": "tok", "client_id": "cid", "client_secret": "s"}]
