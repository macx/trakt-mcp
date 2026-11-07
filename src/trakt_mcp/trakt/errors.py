"""Exceptions raised when talking to Trakt.

Messages are written for the AI assistant that calls our tools: they say what
went wrong and what to do next, and never contain tokens or raw response bodies.
"""

from __future__ import annotations


class TraktError(Exception):
    """Base class for all Trakt-related failures."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class TraktAuthError(TraktError):
    """Trakt rejected the user's token; the user has to reconnect."""


class TraktForbiddenError(TraktError):
    """HTTP 403: the request is not permitted (e.g. a VIP-only feature).

    Deliberately *not* a :class:`TraktAuthError`: it must not trigger a token
    refresh, because Trakt refresh tokens are single use.
    """


class TraktNotFoundError(TraktError):
    """The requested movie, show or episode does not exist on Trakt."""


class TraktLimitError(TraktError):
    """HTTP 420: the user's account limit (e.g. watchlist size) is exhausted."""


class TraktRateLimitError(TraktError):
    """HTTP 429: too many requests; ``retry_after`` is in seconds, if known."""

    def __init__(self, message: str, *, retry_after: int | None = None) -> None:
        super().__init__(message, status=429)
        self.retry_after = retry_after


class TraktUnavailableError(TraktError):
    """Trakt could not be reached or returned a server error."""
