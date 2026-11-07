"""The OAuth authorization server behind the MCP endpoint.

Flow ("Sign in with Trakt", device flow):

1. An MCP client (e.g. claude.ai) registers itself via dynamic client
   registration. Only redirect URIs on the configured allowlist are accepted.
2. ``authorize`` stores the request and sends the browser to our ``/login``
   page.
3. ``/login`` first asks for the **owner passphrase** (a secret only the
   server owner knows). Only then does it obtain a Trakt device code and show
   it; the user enters it at trakt.tv/activate. The page reloads itself and
   polls Trakt until tokens arrive. (The device flow needs no redirect URI, so
   it works with Trakt development apps, which may only redirect to
   localhost.) The passphrase binds the sign-in to the person who started it,
   which defeats device-code phishing: an attacker who tricks the owner into
   entering the attacker's code still cannot get past this page.
4. We ask Trakt who signed in and continue only if that Trakt user is
   allowlisted.
5. **Consent.** Unless this user approved this client before, we show a
   consent page (CSRF-protected). Approval is bound to the pair
   (client, signed-in user). This defeats the "confused deputy" attack: an
   attacker's client may share claude.ai's redirect URI, and the owner could be
   tricked into signing in for it.
6. We mint our own short-lived authorization code and redirect the browser
   back to the MCP client, which exchanges it (with its PKCE verifier) for our
   opaque access and refresh tokens. Refresh tokens rotate; replaying a used
   one revokes the whole token family; no sign-in lives longer than
   ``max_session_seconds``.

Our tokens are random strings; only their SHA-256 hashes are stored.
"""

from __future__ import annotations

import asyncio
import hmac
import ipaddress
import logging
import re
import secrets
from typing import Any
from urllib.parse import urlencode, urlsplit

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyUrl
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

from trakt_mcp.auth.pages import consent_page, device_page, error_page, passphrase_page
from trakt_mcp.auth.passphrase import verify_passphrase
from trakt_mcp.config import Settings, redirect_uri_allowed
from trakt_mcp.storage import Grant, Store, hash_token
from trakt_mcp.trakt.errors import TraktError
from trakt_mcp.trakt.http import DevicePollStatus, TokenResponse, TraktApi, TraktOAuth
from trakt_mcp.trakt.sessions import TraktSessions

logger = logging.getLogger(__name__)

SCOPE = "trakt"
_TOKEN_BYTES = 32  # 256 bits of entropy for every secret we mint
_MAX_STATE_LENGTH = 512
_PKCE_S256_CHALLENGE = re.compile(r"[A-Za-z0-9_-]{43}")
#: A used refresh token presented again within this window is treated as a
#: client retry (e.g. a lost response), not as theft: it is refused, but the
#: token family is kept.
REFRESH_REUSE_GRACE_SECONDS = 10
_MAX_PENDING_PER_CLIENT = 5
_MIN_POLL_SECONDS = 5
# The name of a settings key (the value is a scrypt hash), not a secret:
PASSPHRASE_SETTING = "owner_passphrase"  # noqa: S105  # nosec B105
MAX_FAILURES_PER_SIGN_IN = 5
#: Wrong passphrases allowed per client IP and hour (needs TRUSTED_PROXIES behind a
#: reverse proxy, otherwise every request has the proxy's IP).
MAX_FAILURES_PER_IP = 10
#: Server-wide ceiling per hour. An attacker with many IPs can use it to block
#: sign-ins for an hour, but never to guess the passphrase.
MAX_FAILURES = 100
FAILURE_WINDOW_SECONDS = 3600
_IPV6 = 6


def _new_secret() -> str:
    return secrets.token_urlsafe(_TOKEN_BYTES)


class TraktAuthProvider(
    OAuthAuthorizationServerProvider[AuthorizationCode, RefreshToken, AccessToken]
):
    """Our implementation of the SDK's authorization server provider protocol.

    The enterprise identity-assertion grant (SEP-990) is not offered; the
    protocol's default implementation rejects it.
    """

    def __init__(
        self,
        settings: Settings,
        store: Store,
        trakt_oauth: TraktOAuth,
        trakt_api: TraktApi,
        sessions: TraktSessions,
    ) -> None:
        self._settings = settings
        self._store = store
        self._trakt_oauth = trakt_oauth
        self._trakt_api = trakt_api
        self._sessions = sessions
        self._login_locks: dict[str, asyncio.Lock] = {}
        self._passphrase_lock = asyncio.Lock()

    # -- client registration ------------------------------------------------

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        metadata = await self._store.get_client(client_id)
        return OAuthClientInformationFull.model_validate(metadata) if metadata else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        uris = [str(uri) for uri in client_info.redirect_uris or []]
        if not uris:
            raise RegistrationError("invalid_redirect_uri", "At least one redirect_uri is required")
        rejected = [
            u for u in uris if not redirect_uri_allowed(u, self._settings.allowed_redirect_uris)
        ]
        if rejected:
            logger.warning("Rejected client registration for redirect URIs %s", rejected)
            raise RegistrationError(
                "invalid_redirect_uri", "redirect_uri is not allowed by this server"
            )
        if client_info.client_id is None:  # pragma: no cover - the SDK always assigns one
            raise RegistrationError("invalid_client_metadata", "client_id missing")
        await self._purge()
        # Only an allowlisted user can approve a client, so approved clients are
        # trustworthy. Unapproved ones may be junk: evict the oldest rather than
        # refusing, so a registration flood cannot lock the owner out.
        await self._store.evict_unapproved_clients(keep=self._settings.max_registered_clients - 1)
        await self._store.save_client(client_info.client_id, client_info.model_dump(mode="json"))
        name = (client_info.client_name or "")[:80]
        logger.info("Registered MCP client %s (%r)", client_info.client_id, name)

    # -- authorization ------------------------------------------------------

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        if params.resource is not None and not self._is_own_resource(params.resource):
            raise AuthorizeError("invalid_target", "Unknown resource")
        if params.state is not None and len(params.state) > _MAX_STATE_LENGTH:
            raise AuthorizeError("invalid_request", "state is too long")
        if not _PKCE_S256_CHALLENGE.fullmatch(params.code_challenge):
            raise AuthorizeError("invalid_request", "code_challenge must be an S256 challenge")
        if client.client_id is None:  # pragma: no cover - registered clients always have one
            raise AuthorizeError("unauthorized_client", "Unknown client")
        await self._purge()
        request_id = _new_secret()
        await self._store.save_pending_login(
            request_id,
            client.client_id,
            {
                "stage": "device",
                "client_id": client.client_id,
                "params": params.model_dump(mode="json"),
                "unlocked": False,
                "failures": 0,
            },
            ttl=self._settings.pending_login_ttl_seconds,
            max_per_client=_MAX_PENDING_PER_CLIENT,
            max_total=self._settings.max_pending_logins,
        )
        return f"{self._settings.base_url}/login?{urlencode({'request': request_id})}"

    def _is_own_resource(self, resource: str) -> bool:
        base = self._settings.base_url
        return resource.rstrip("/") in (base, self._settings.mcp_url)

    # -- Trakt device sign-in -------------------------------------------------

    async def handle_login(self, request: Request) -> Response:
        """The sign-in page: passphrase first, then the Trakt code until approved.

        GET shows the passphrase form, or (once unlocked) the device code; the
        page reloads itself and each reload polls Trakt, never faster than
        Trakt's interval. POST checks the passphrase.
        """
        if request.method == "POST":
            form = await request.form()
            request_id = str(form.get("request", ""))
            passphrase = str(form.get("passphrase", ""))
        else:
            request_id, passphrase = request.query_params.get("request", ""), ""
        browser = request.cookies.get(self._login_cookie, "")
        pending = await self._store.get_pending_login(request_id) if request_id else None
        if pending is None or pending.get("stage") != "device":
            return _expired()
        # Serialise work on the same sign-in (e.g. quick reloads) so Trakt is
        # polled at most once at a time; locks exist only for live sign-ins.
        lock = self._login_locks.setdefault(request_id, asyncio.Lock())
        try:
            async with lock:
                pending = await self._store.get_pending_login(request_id)
                if pending is None:
                    return _expired()
                if request.method == "POST":
                    ip = _client_ip(request, self._settings)
                    return await self._check_passphrase(request_id, pending, passphrase, ip)
                return await self._login_step(request_id, pending, browser)
        finally:
            if await self._store.get_pending_login(request_id) is None:
                self._login_locks.pop(request_id, None)

    @property
    def _login_cookie(self) -> str:
        secure = self._settings.base_url.startswith("https://")
        return "__Host-trakt-mcp-login" if secure else "trakt-mcp-login"

    async def _check_passphrase(
        self, request_id: str, pending: dict[str, Any], passphrase: str, ip: str
    ) -> Response:
        stored = await self._store.get_setting(PASSPHRASE_SETTING)
        if stored is None:
            return _not_configured()
        # Check, verify and record under one lock: attempts are strictly serialised,
        # so parallel requests cannot slip past the limits.
        async with self._passphrase_lock:
            window = FAILURE_WINDOW_SECONDS
            if (
                await self._store.count_recent_failures(window, ip) >= MAX_FAILURES_PER_IP
                or await self._store.count_recent_failures(window) >= MAX_FAILURES
            ):
                logger.warning("Passphrase attempts are rate limited (ip %s)", ip)
                return error_page("Too many wrong passphrases. Please try again in an hour.")
            valid = await asyncio.to_thread(verify_passphrase, passphrase, stored)
            if not valid:
                await self._store.record_failure(ip)
        if not valid:
            failures = int(pending.get("failures", 0)) + 1
            logger.warning("Wrong passphrase for a sign-in (attempt %d, ip %s)", failures, ip)
            if failures >= MAX_FAILURES_PER_SIGN_IN:
                await self._store.pop_pending_login(request_id)
                return error_page("Too many wrong passphrases. Start connecting again.")
            await self._store.update_pending_login(request_id, {**pending, "failures": failures})
            return await self._passphrase_form(request_id, pending, error="Wrong passphrase.")
        # Bind the rest of the sign-in to *this* browser: whoever else knows the
        # request ID (e.g. an attacker who sent the owner the link) cannot continue it.
        browser = _new_secret()
        await self._store.update_pending_login(
            request_id, {**pending, "unlocked": True, "browser_hash": hash_token(browser)}
        )
        response = RedirectResponse(
            f"/login?{urlencode({'request': request_id})}",
            status_code=303,
            headers={"Cache-Control": "no-store"},
        )
        response.set_cookie(
            self._login_cookie,
            browser,
            max_age=self._settings.pending_login_ttl_seconds,
            path="/",
            secure=self._login_cookie.startswith("__Host-"),
            httponly=True,
            samesite="strict",
        )
        return response

    async def _login_step(self, request_id: str, pending: dict[str, Any], browser: str) -> Response:
        if not pending.get("unlocked"):
            return await self._passphrase_form(request_id, pending, error=None)
        if not _same_browser(browser, pending):
            logger.warning("Sign-in %s continued from a different browser", request_id[:8])
            return error_page("This sign-in was started in another browser. Please start again.")
        return await self._device_step(request_id, pending)

    async def _device_step(self, request_id: str, pending: dict[str, Any]) -> Response:
        if "device_code" not in pending:
            return await self._start_device_flow(request_id, pending)
        now = self._store.now()
        if now >= int(pending["device_expires_at"]):
            await self._store.pop_pending_login(request_id)
            return _expired()
        if now < int(pending["next_poll_at"]):
            return self._device_page(pending)

        try:
            result = await self._trakt_oauth.poll_device_token(pending["device_code"])
        except TraktError:
            logger.warning("Polling Trakt for the device token failed", exc_info=True)
            result = DevicePollStatus.PENDING
        if isinstance(result, DevicePollStatus):
            return await self._still_waiting(request_id, pending, result)
        # Single use from here on: whatever happens next, this login is done.
        await self._store.pop_pending_login(request_id)
        params = AuthorizationParams.model_validate(pending["params"])
        return await self._signed_in(
            str(pending["client_id"]), params, result, str(pending["browser_hash"])
        )

    async def _passphrase_form(
        self, request_id: str, pending: dict[str, Any], *, error: str | None
    ) -> Response:
        if await self._store.get_setting(PASSPHRASE_SETTING) is None:
            return _not_configured()
        client = await self.get_client(str(pending["client_id"]))
        params = AuthorizationParams.model_validate(pending["params"])
        return passphrase_page(
            request_id=request_id,
            error=error,
            client_name=((client.client_name if client else None) or "Unnamed application")[:100],
            redirect_host=urlsplit(str(params.redirect_uri)).netloc,
        )

    async def _start_device_flow(self, request_id: str, pending: dict[str, Any]) -> Response:
        try:
            device = await self._trakt_oauth.device_code()
        except TraktError:
            logger.exception("Could not start the Trakt device sign-in")
            return error_page("Trakt is not reachable right now. Please try again later.")
        now = self._store.now()
        interval = max(device.interval, _MIN_POLL_SECONDS)
        pending |= {
            "device_code": device.device_code,
            "user_code": device.user_code,
            "verification_url": device.verification_url,
            "interval": interval,
            "device_expires_at": now + device.expires_in,
            "next_poll_at": now + interval,
        }
        await self._store.update_pending_login(request_id, pending)
        return self._device_page(pending)

    async def _still_waiting(
        self, request_id: str, pending: dict[str, Any], status: DevicePollStatus
    ) -> Response:
        if status in (DevicePollStatus.DENIED, DevicePollStatus.EXPIRED):
            await self._store.pop_pending_login(request_id)
            logger.info("Trakt device sign-in ended: %s", status.value)
            params = AuthorizationParams.model_validate(pending["params"])
            return _back_to_client(
                params, error="access_denied", error_description="Trakt sign-in was not completed"
            )
        interval = int(pending["interval"])
        if status is DevicePollStatus.SLOW_DOWN:
            interval += _MIN_POLL_SECONDS
        pending |= {"interval": interval, "next_poll_at": self._store.now() + interval}
        await self._store.update_pending_login(request_id, pending)
        return self._device_page(pending)

    def _device_page(self, pending: dict[str, Any]) -> Response:
        return device_page(
            user_code=str(pending["user_code"]),
            verification_url=str(pending["verification_url"]),
            refresh_seconds=int(pending["interval"]),
        )

    async def _signed_in(
        self,
        client_id: str,
        params: AuthorizationParams,
        token: TokenResponse,
        browser_hash: str,
    ) -> Response:
        try:
            settings = await self._trakt_api.request(
                "GET", "/users/settings", access_token=token.access_token
            )
            subject = _trakt_slug(settings)
        except (TraktError, KeyError, TypeError, ValueError):
            logger.exception("Could not read the Trakt account after sign-in")
            return _back_to_client(
                params, error="server_error", error_description="Trakt sign-in failed"
            )

        if subject not in self._settings.allowed_trakt_users:
            logger.warning("Trakt user %r is not on the allowlist", subject[:80])
            await self._trakt_oauth.revoke(token.access_token)
            return _back_to_client(
                params, error="access_denied", error_description="This Trakt account is not allowed"
            )

        await self._sessions.save(subject, token)
        if await self._store.client_approved(client_id, subject):
            return await self._complete(client_id, subject, params)
        return await self._ask_consent(client_id, subject, params, browser_hash)

    # -- consent ------------------------------------------------------------

    @property
    def _csrf_cookie(self) -> str:
        # The __Host- prefix pins the cookie to this host, path / and HTTPS.
        secure = self._settings.base_url.startswith("https://")
        return "__Host-trakt-mcp-consent" if secure else "trakt-mcp-consent"

    async def _ask_consent(
        self, client_id: str, subject: str, params: AuthorizationParams, browser_hash: str
    ) -> Response:
        """Show who is asking for access. Only reachable after an allowlisted sign-in."""
        client = await self.get_client(client_id)
        if client is None:
            return _expired()
        consent_id, csrf = _new_secret(), _new_secret()
        await self._store.save_pending_login(
            consent_id,
            client_id,
            {
                "stage": "consent",
                "client_id": client_id,
                "subject": subject,
                "params": params.model_dump(mode="json"),
                "csrf_hash": hash_token(csrf),
                "browser_hash": browser_hash,
            },
            ttl=self._settings.pending_login_ttl_seconds,
            stage="consent",
            max_per_client=_MAX_PENDING_PER_CLIENT,
            max_total=self._settings.max_pending_logins,
        )
        redirect = urlsplit(str(params.redirect_uri))
        response = consent_page(
            request_id=consent_id,
            csrf_token=csrf,
            client_name=(client.client_name or "Unnamed application")[:100],
            redirect_host=redirect.netloc,
            registered_at=await self._store.client_created_at(client_id) or 0,
            account=subject,
            # form-action also governs the redirect after the POST (to the client).
            form_target=f"{redirect.scheme}://{redirect.netloc}",
            now=self._store.now(),
        )
        response.set_cookie(
            self._csrf_cookie,
            csrf,
            max_age=self._settings.pending_login_ttl_seconds,
            path="/",
            secure=self._csrf_cookie.startswith("__Host-"),
            httponly=True,
            samesite="strict",
        )
        return response

    async def handle_consent_decision(self, request: Request) -> Response:
        form = await request.form()
        consent_id = str(form.get("request", ""))
        submitted = str(form.get("csrf", ""))
        cookie = request.cookies.get(self._csrf_cookie, "")
        pending = await self._store.pop_pending_login(consent_id) if consent_id else None
        if (
            pending is None
            or pending.get("stage") != "consent"
            or not submitted
            or not hmac.compare_digest(submitted, cookie)
            or not hmac.compare_digest(hash_token(submitted), pending.get("csrf_hash", ""))
            or not _same_browser(request.cookies.get(self._login_cookie, ""), pending)
        ):
            logger.warning("Rejected a consent decision that failed validation")
            return error_page(
                "This request could not be verified. If you already allowed access a moment "
                "ago, you are connected and can close this window. Otherwise, start again "
                "from your AI app."
            )

        params = AuthorizationParams.model_validate(pending["params"])
        client_id, subject = str(pending["client_id"]), str(pending["subject"])
        if form.get("action") == "approve":
            await self._store.approve_client(client_id, subject)
            logger.info("User %s approved client %s", subject, client_id)
            response = await self._complete(client_id, subject, params)
        else:
            logger.info("User %s denied client %s", subject, client_id)
            response = _back_to_client(
                params, error="access_denied", error_description="The user denied access"
            )
        response.delete_cookie(self._csrf_cookie, path="/")
        response.delete_cookie(self._login_cookie, path="/")
        return response

    async def _complete(
        self, client_id: str, subject: str, params: AuthorizationParams
    ) -> Response:
        """Mint our authorization code and send the browser back to the client."""
        our_code = _new_secret()
        now = self._store.now()
        await self._store.save_grant(
            our_code,
            Grant(
                kind="code",
                client_id=client_id,
                subject=subject,
                payload={
                    "scopes": params.scopes or [SCOPE],
                    "code_challenge": params.code_challenge,
                    "redirect_uri": str(params.redirect_uri),
                    "redirect_uri_provided_explicitly": params.redirect_uri_provided_explicitly,
                    "session_expires_at": now + self._settings.max_session_seconds,
                },
                family=_new_secret(),
                expires_at=now + self._settings.authorization_code_ttl_seconds,
            ),
        )
        logger.info("Trakt user %s signed in via client %s", subject, client_id)
        return _back_to_client(params, code=our_code)

    # -- authorization code -------------------------------------------------

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        grant = await self._store.get_grant(authorization_code, "code")
        if grant is None or grant.client_id != client.client_id:
            return None
        return AuthorizationCode(
            code=authorization_code,
            scopes=grant.payload["scopes"],
            expires_at=float(grant.expires_at),
            client_id=grant.client_id,
            code_challenge=grant.payload["code_challenge"],
            redirect_uri=AnyUrl(grant.payload["redirect_uri"]),
            redirect_uri_provided_explicitly=grant.payload["redirect_uri_provided_explicitly"],
            resource=self._settings.mcp_url,
            subject=grant.subject,
        )

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        grant = await self._store.consume_grant(authorization_code.code, "code")
        if grant is None or grant.client_id != client.client_id:
            raise TokenError("invalid_grant", "Authorization code is invalid or was already used")
        return await self._issue_tokens(grant, grant.payload["scopes"])

    # -- refresh tokens -----------------------------------------------------

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        grant = await self._store.get_grant(refresh_token, "refresh")
        if grant is None:
            used = await self._store.get_grant(refresh_token, "refresh_used")
            in_grace = (
                used is not None
                and used.used_at is not None
                and self._store.now() - used.used_at <= REFRESH_REUSE_GRACE_SECONDS
            )
            if used is not None and not in_grace:
                logger.warning("Replay of a used refresh token; revoking its token family")
                await self._store.revoke_family(used.family)
            return None
        if grant.client_id != client.client_id:
            return None
        return RefreshToken(
            token=refresh_token,
            client_id=grant.client_id,
            scopes=grant.payload["scopes"],
            expires_at=grant.expires_at,
            resource=self._settings.mcp_url,
            subject=grant.subject,
        )

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        grant = await self._store.retire_refresh_token(refresh_token.token)
        if grant is None or grant.client_id != client.client_id:
            raise TokenError("invalid_grant", "Refresh token is invalid or was already used")
        granted = grant.payload["scopes"]
        if any(scope not in granted for scope in scopes):
            raise TokenError("invalid_scope", "Requested scope exceeds the original grant")
        return await self._issue_tokens(grant, scopes or granted)

    # -- access tokens ------------------------------------------------------

    async def load_access_token(self, token: str) -> AccessToken | None:
        grant = await self._store.get_grant(token, "access")
        if grant is None or not self._still_allowed(grant):
            return None
        return AccessToken(
            token=token,
            client_id=grant.client_id,
            scopes=grant.payload["scopes"],
            expires_at=grant.expires_at,
            resource=self._settings.mcp_url,
            subject=grant.subject,
        )

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        family = await self._store.family_of(token.token)
        if family is not None:
            await self._store.revoke_family(family)

    # -- helpers ------------------------------------------------------------

    def _still_allowed(self, grant: Grant) -> bool:
        """Re-check the allowlist on every request, so removing a user takes effect at once."""
        return grant.subject in self._settings.allowed_trakt_users

    async def _purge(self) -> None:
        await self._store.purge_expired(self._settings.unapproved_client_ttl_seconds)

    async def _issue_tokens(self, parent: Grant, scopes: list[str]) -> OAuthToken:
        """Mint a new access/refresh token pair in the same family as ``parent``.

        Neither token outlives the sign-in's absolute session lifetime.
        """
        now = self._store.now()
        session_end = int(parent.payload["session_expires_at"])
        if session_end <= now:
            raise TokenError("invalid_grant", "The sign-in has expired; please sign in again")
        access, refresh = _new_secret(), _new_secret()
        access_expires = min(now + self._settings.access_token_ttl_seconds, session_end)
        refresh_expires = min(now + self._settings.refresh_token_ttl_seconds, session_end)
        payload: dict[str, Any] = {"scopes": scopes, "session_expires_at": session_end}
        await self._store.save_grant(
            access,
            Grant(
                "access",
                parent.client_id,
                parent.subject,
                payload,
                parent.family,
                access_expires,
            ),
        )
        await self._store.save_grant(
            refresh,
            Grant(
                "refresh",
                parent.client_id,
                parent.subject,
                payload,
                parent.family,
                refresh_expires,
            ),
        )
        return OAuthToken(
            access_token=access,
            expires_in=access_expires - now,
            scope=" ".join(scopes),
            refresh_token=refresh,
        )


def _trakt_slug(settings: Any) -> str:
    """The user's Trakt slug (as in trakt.tv/users/<slug>), which we use as the subject.

    Only the slug is matched against the allowlist - not the display name.
    """
    slug = str(settings["user"]["ids"]["slug"]).strip().lower()
    if not slug:
        raise ValueError("Trakt user has no slug")
    return slug


def _back_to_client(params: AuthorizationParams, **query: str | None) -> Response:
    """Redirect to the client's (registered, validated) redirect URI with its state."""
    return RedirectResponse(
        construct_redirect_uri(str(params.redirect_uri), state=params.state, **query),
        status_code=302,
        headers={"Cache-Control": "no-store"},
    )


def _same_browser(cookie: str, pending: dict[str, Any]) -> bool:
    """Whether ``cookie`` is the browser secret set when this sign-in was unlocked."""
    expected = str(pending.get("browser_hash", ""))
    return bool(cookie and expected) and hmac.compare_digest(hash_token(cookie), expected)


def _client_ip(request: Request, settings: Settings) -> str:
    """The client's IP, honouring X-Forwarded-For only from trusted proxies."""
    peer = request.client.host if request.client else ""
    hops = [peer] + [
        h.strip() for h in reversed(request.headers.get("x-forwarded-for", "").split(","))
    ]
    for hop in hops:  # walk from the nearest hop outwards, skipping trusted proxies
        try:
            address = ipaddress.ip_address(hop)
        except ValueError:
            return hop[:64]
        if not any(address in net for net in settings.trusted_proxies):
            if address.version == _IPV6:
                # One IPv6 user typically owns a whole /64: rate limit it as one.
                return str(ipaddress.ip_network(f"{address}/64", strict=False))
            return str(address)
    return peer


def _not_configured() -> Response:
    return error_page(
        "Sign-in is not set up yet. The server owner has to set a passphrase first: "
        "docker exec -it trakt-mcp trakt-mcp set-passphrase"
    )


def _expired() -> Response:
    return error_page(
        "This sign-in link has expired or was already used. "
        "Please start connecting again from your AI app."
    )
