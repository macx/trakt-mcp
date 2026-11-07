"""HTTP clients for Trakt's OAuth host and REST API.

Reference: https://github.com/trakt/api-help (the official typed contracts).
OAuth requests go to ``auth.trakt.tv``; API requests to ``api.trakt.tv``.
"""

from __future__ import annotations

import enum
import logging
from dataclasses import dataclass
from typing import Any

import httpx

from trakt_mcp import __version__
from trakt_mcp.trakt.errors import (
    TraktAuthError,
    TraktError,
    TraktForbiddenError,
    TraktLimitError,
    TraktNotFoundError,
    TraktRateLimitError,
    TraktUnavailableError,
)

logger = logging.getLogger(__name__)

USER_AGENT = f"trakt-mcp/{__version__}"
API_VERSION = "2"
_TIMEOUT = httpx.Timeout(15.0, connect=5.0)

# Trakt-specific status codes, see "Status Codes" in the API docs.
_HTTP_ACCOUNT_LIMIT = 420
_HTTP_RATE_LIMIT = 429
_HTTP_SERVER_ERROR = 500


@dataclass(frozen=True, slots=True)
class TokenResponse:
    access_token: str
    refresh_token: str
    expires_in: int
    created_at: int

    @property
    def expires_at(self) -> int:
        return self.created_at + self.expires_in


@dataclass(frozen=True, slots=True)
class DeviceCode:
    """Codes for Trakt's device flow (RFC 8628)."""

    device_code: str
    user_code: str
    verification_url: str
    expires_in: int
    interval: int


class DevicePollStatus(enum.Enum):
    """Outcome of polling for a device token that is not (yet) a token."""

    PENDING = "pending"  # 400: the user has not approved yet
    SLOW_DOWN = "slow_down"  # 429: we polled too fast
    DENIED = "denied"  # 418: the user explicitly denied
    EXPIRED = "expired"  # 404 / 409 / 410: invalid, already used or expired


_DEVICE_STATUS = {
    400: DevicePollStatus.PENDING,
    404: DevicePollStatus.EXPIRED,
    409: DevicePollStatus.EXPIRED,
    410: DevicePollStatus.EXPIRED,
    418: DevicePollStatus.DENIED,
    429: DevicePollStatus.SLOW_DOWN,
}


def new_http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=_TIMEOUT,
        headers={"User-Agent": USER_AGENT},
        follow_redirects=False,
    )


class TraktOAuth:
    """Device flow and token maintenance against ``auth.trakt.tv``.

    The device flow needs no redirect URI, which is what makes it work with
    Trakt *development* apps (they may only redirect to localhost).
    """

    def __init__(
        self,
        http: httpx.AsyncClient,
        *,
        auth_url: str,
        client_id: str,
        client_secret: str | None,
        redirect_uri: str,
    ) -> None:
        self._http = http
        self._auth_url = auth_url.rstrip("/")
        self._client_id = client_id
        self._client_secret = client_secret
        # Trakt's token endpoint expects a redirect_uri on refresh; it must be
        # one registered for the app.
        self._redirect_uri = redirect_uri

    async def device_code(self) -> DeviceCode:
        try:
            response = await self._http.post(
                f"{self._auth_url}/oauth/device/code", json={"client_id": self._client_id}
            )
        except httpx.HTTPError as exc:
            raise TraktUnavailableError("Could not reach Trakt to sign in.") from exc
        if response.status_code != httpx.codes.OK:
            logger.warning("Trakt device code endpoint returned HTTP %s", response.status_code)
            _raise_for_status(response)
            raise TraktError("Trakt did not issue a device code.")
        data = response.json()
        try:
            return DeviceCode(
                device_code=str(data["device_code"]),
                user_code=str(data["user_code"]),
                verification_url=str(data["verification_url"]),
                expires_in=int(data["expires_in"]),
                interval=int(data["interval"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise TraktError("Trakt returned an unexpected device code response.") from exc

    async def poll_device_token(self, device_code: str) -> TokenResponse | DevicePollStatus:
        body = {"code": device_code, "client_id": self._client_id}
        if self._client_secret:
            body["client_secret"] = self._client_secret
        try:
            response = await self._http.post(f"{self._auth_url}/oauth/device/token", json=body)
        except httpx.HTTPError as exc:
            raise TraktUnavailableError("Could not reach Trakt to sign in.") from exc
        if response.status_code == httpx.codes.OK:
            return _token_response(response)
        if response.status_code in _DEVICE_STATUS:
            return _DEVICE_STATUS[response.status_code]
        logger.warning("Trakt device token endpoint returned HTTP %s", response.status_code)
        raise TraktUnavailableError("Trakt sign-in is temporarily unavailable.")

    async def refresh(self, refresh_token: str) -> TokenResponse:
        return await self._token_request(
            {"grant_type": "refresh_token", "refresh_token": refresh_token}
        )

    async def revoke(self, access_token: str) -> None:
        """Best effort: tell Trakt to forget a token we will not use."""
        body = {"token": access_token, "client_id": self._client_id}
        if self._client_secret:
            body["client_secret"] = self._client_secret
        try:
            await self._http.post(f"{self._auth_url}/oauth/revoke", json=body)
        except httpx.HTTPError:
            logger.warning("Could not revoke a Trakt token", exc_info=True)

    async def _token_request(self, fields: dict[str, str]) -> TokenResponse:
        body = {"client_id": self._client_id, "redirect_uri": self._redirect_uri, **fields}
        if self._client_secret:
            body["client_secret"] = self._client_secret
        try:
            response = await self._http.post(f"{self._auth_url}/oauth/token", json=body)
        except httpx.HTTPError as exc:
            raise TraktUnavailableError("Could not reach Trakt to sign in.") from exc
        if response.status_code >= _HTTP_SERVER_ERROR:
            raise TraktUnavailableError("Trakt sign-in is temporarily unavailable.")
        if response.status_code != httpx.codes.OK:
            # 400/401 with an OAuth error: the code or refresh token is no longer valid.
            logger.warning("Trakt token endpoint returned HTTP %s", response.status_code)
            raise TraktAuthError("Trakt rejected the sign-in.", status=response.status_code)
        return _token_response(response)


def _token_response(response: httpx.Response) -> TokenResponse:
    try:
        data = response.json()
        return TokenResponse(
            access_token=str(data["access_token"]),
            refresh_token=str(data["refresh_token"]),
            expires_in=int(data["expires_in"]),
            created_at=int(data["created_at"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise TraktError("Trakt returned an unexpected token response.") from exc


class TraktApi:
    """Authenticated JSON requests against ``api.trakt.tv``."""

    def __init__(self, http: httpx.AsyncClient, *, api_url: str, client_id: str) -> None:
        self._http = http
        self._api_url = api_url.rstrip("/")
        self._client_id = client_id

    async def request(
        self,
        method: str,
        path: str,
        *,
        access_token: str,
        params: dict[str, Any] | None = None,
        json: Any = None,
    ) -> Any:
        if not path.startswith("/"):
            raise ValueError("path must start with '/'")
        headers = {
            "Content-Type": "application/json",
            "trakt-api-version": API_VERSION,
            "trakt-api-key": self._client_id,
            "Authorization": f"Bearer {access_token}",
        }
        try:
            response = await self._http.request(
                method, f"{self._api_url}{path}", params=params, json=json, headers=headers
            )
        except httpx.HTTPError as exc:
            raise TraktUnavailableError("Could not reach Trakt. Try again shortly.") from exc
        _raise_for_status(response)
        if response.status_code == httpx.codes.NO_CONTENT or not response.content:
            return None
        return response.json()


def _raise_for_status(response: httpx.Response) -> None:
    status = response.status_code
    if status < httpx.codes.BAD_REQUEST:
        return
    logger.info("Trakt API %s %s -> HTTP %s", response.request.method, response.url.path, status)
    if status == httpx.codes.UNAUTHORIZED:
        raise TraktAuthError("Trakt rejected the access token.", status=status)
    if status == httpx.codes.FORBIDDEN:
        raise TraktForbiddenError("Trakt does not permit this request.", status=status)
    if status == httpx.codes.NOT_FOUND:
        raise TraktNotFoundError("Not found on Trakt.", status=status)
    if status == _HTTP_ACCOUNT_LIMIT:
        raise TraktLimitError("The Trakt account limit for this list is reached.", status=status)
    if status == _HTTP_RATE_LIMIT:
        retry_after = response.headers.get("Retry-After", "")
        raise TraktRateLimitError(
            "Trakt rate limit reached.",
            retry_after=int(retry_after) if retry_after.isdigit() else None,
        )
    if status >= _HTTP_SERVER_ERROR:
        raise TraktUnavailableError("Trakt is temporarily unavailable.", status=status)
    raise TraktError(f"Trakt rejected the request (HTTP {status}).", status=status)
