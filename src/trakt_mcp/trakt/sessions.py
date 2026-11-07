"""Keeps each user's Trakt access token fresh.

Trakt access tokens live 24 hours and refresh tokens are single-use: every
refresh returns a new pair and invalidates the old refresh token. Two
concurrent refreshes would therefore lock the user out, so refreshes are
serialised per user.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from collections.abc import Awaitable, Callable

from trakt_mcp.storage import Store, TraktTokens
from trakt_mcp.trakt.errors import TraktAuthError
from trakt_mcp.trakt.http import TokenResponse, TraktOAuth

logger = logging.getLogger(__name__)

#: Refresh proactively when the token expires within this many seconds.
REFRESH_MARGIN_SECONDS = 300


class ReconnectRequiredError(TraktAuthError):
    """The stored Trakt authorisation is gone; the user must sign in again."""

    def __init__(self) -> None:
        super().__init__(
            "The Trakt connection has expired. Ask the user to reconnect the Trakt "
            "connector in their AI app's settings."
        )


class TraktSessions:
    def __init__(self, store: Store, oauth: TraktOAuth) -> None:
        self._store = store
        self._oauth = oauth
        self._locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def save(self, subject: str, token: TokenResponse) -> None:
        await self._store.save_trakt_tokens(
            subject, TraktTokens(token.access_token, token.refresh_token, token.expires_at)
        )

    async def access_token(self, subject: str, *, force_refresh: bool = False) -> str:
        """Return a valid access token for ``subject``, refreshing it if needed.

        ``force_refresh`` is used after Trakt rejected a token that still looked
        valid locally (e.g. revoked on trakt.tv).
        """
        async with self._locks[subject]:
            tokens = await self._store.get_trakt_tokens(subject)
            if tokens is None:
                raise ReconnectRequiredError
            expiring = tokens.expires_at - self._store.now() <= REFRESH_MARGIN_SECONDS
            if not (force_refresh or expiring):
                return tokens.access_token
            try:
                fresh = await self._oauth.refresh(tokens.refresh_token)
            except TraktAuthError:
                logger.warning("Trakt refresh token rejected for %s; reconnect required", subject)
                await self.disconnect(subject)
                raise ReconnectRequiredError from None
            await self.save(subject, fresh)
            return fresh.access_token

    async def call[T](self, subject: str, fn: Callable[[str], Awaitable[T]]) -> T:
        """Run ``fn(access_token)``; on a rejected token refresh once and retry."""
        token = await self.access_token(subject)
        try:
            return await fn(token)
        except ReconnectRequiredError:
            raise
        except TraktAuthError:
            token = await self.access_token(subject, force_refresh=True)
            return await fn(token)

    async def disconnect(self, subject: str) -> None:
        """Forget the user's Trakt tokens and every MCP token issued to them."""
        await self._store.delete_trakt_tokens(subject)
        await self._store.revoke_subject(subject)
