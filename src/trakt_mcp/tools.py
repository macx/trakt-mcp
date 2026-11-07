"""The MCP tools. Kept deliberately small: what an assistant needs in a chat.

Each tool resolves the signed-in Trakt user from the request's access token,
delegates to :class:`~trakt_mcp.trakt.service.TraktService` and turns Trakt
errors into short, actionable messages for the model.
"""

from __future__ import annotations

import functools
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Annotated, Any, Literal

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations
from pydantic import BaseModel, Field

from trakt_mcp.models import (
    History,
    HistoryChange,
    ListChange,
    MediaRef,
    MediaType,
    SearchResult,
    ShowProgress,
    UpNext,
    Watchlist,
    WatchStats,
)
from trakt_mcp.trakt.errors import (
    TraktAuthError,
    TraktError,
    TraktLimitError,
    TraktNotFoundError,
    TraktRateLimitError,
)
from trakt_mcp.trakt.service import TraktService

logger = logging.getLogger(__name__)

MAX_ITEMS_PER_CALL = 20

SERVER_INSTRUCTIONS = """\
Manage the user's Trakt account: watchlist, watch progress and statistics.

- To add or remove something, first call `search` and use the `trakt_id` of the
  matching result. If several results could match (remakes, same title in
  different years), ask the user which one they mean before changing anything.
- `mark_watched` adds a play to the history; Trakt does not deduplicate plays,
  so only call it when the user said they watched something.
- Answer the user in their own language; titles may be returned in English.
"""


class EpisodeWatch(BaseModel):
    show_trakt_id: int = Field(gt=0, description="Trakt ID of the show.")
    season: int = Field(ge=0)
    episode: int = Field(ge=1)


ServiceFor = Callable[[str], TraktService]


def register_tools(server: MCPServer, service_for: ServiceFor) -> None:
    """Register all tools on ``server``; ``service_for(subject)`` builds the Trakt service."""

    def current_service() -> TraktService:
        token = get_access_token()
        if token is None or not token.subject:
            # The bearer middleware guarantees a token; this is defence in depth.
            raise ToolError("Not signed in. Reconnect the Trakt connector.")
        return service_for(token.subject)

    def trakt_errors[**P, R](fn: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
        @functools.wraps(fn)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            try:
                return await fn(*args, **kwargs)
            except TraktError as exc:
                raise ToolError(await _describe(exc, current_service)) from exc

        return wrapper

    read_only = ToolAnnotations(readOnlyHint=True, openWorldHint=True)

    @server.tool(title="Search movies and shows", annotations=read_only)
    @trakt_errors
    async def search(
        query: Annotated[str, Field(min_length=1, max_length=200, description="Title to find.")],
        type: Annotated[MediaType | None, Field(description="Limit to movies or shows.")] = None,
        year: Annotated[int | None, Field(ge=1870, le=2100)] = None,
        limit: Annotated[int, Field(ge=1, le=10)] = 5,
    ) -> SearchResult:
        """Find movies or shows on Trakt by title. Returns Trakt IDs for the other tools."""
        return await current_service().search(query, type, year, limit)

    @server.tool(title="Get watchlist", annotations=read_only)
    @trakt_errors
    async def get_watchlist(
        type: Annotated[MediaType | None, Field(description="Only movies or only shows.")] = None,
        limit: Annotated[int, Field(ge=1, le=200)] = 50,
    ) -> Watchlist:
        """List the user's watchlist, newest first, with its size and the account's limit."""
        return await current_service().watchlist(type, limit)

    @server.tool(
        title="Add to watchlist",
        annotations=ToolAnnotations(
            readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True
        ),
    )
    @trakt_errors
    async def add_to_watchlist(
        items: Annotated[list[MediaRef], Field(min_length=1, max_length=MAX_ITEMS_PER_CALL)],
    ) -> ListChange:
        """Add movies or shows (by Trakt ID from `search`) to the user's watchlist."""
        return await current_service().add_to_watchlist(items)

    @server.tool(
        title="Remove from watchlist",
        annotations=ToolAnnotations(
            readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=True
        ),
    )
    @trakt_errors
    async def remove_from_watchlist(
        items: Annotated[list[MediaRef], Field(min_length=1, max_length=MAX_ITEMS_PER_CALL)],
    ) -> ListChange:
        """Remove movies or shows (by Trakt ID) from the user's watchlist."""
        return await current_service().remove_from_watchlist(items)

    @server.tool(title="Show progress", annotations=read_only)
    @trakt_errors
    async def get_show_progress(
        show_trakt_id: Annotated[int, Field(gt=0, description="Trakt ID of the show.")],
    ) -> ShowProgress:
        """How many episodes of a show the user has watched, how many are left, and the
        next episode to watch - overall and per season."""
        return await current_service().show_progress(show_trakt_id)

    @server.tool(title="Watch history", annotations=read_only)
    @trakt_errors
    async def get_history(
        type: Annotated[
            Literal["movies", "episodes"] | None,
            Field(description="Only movies or only episodes; default both."),
        ] = None,
        limit: Annotated[int, Field(ge=1, le=50)] = 10,
    ) -> History:
        """What the user watched most recently (movies and episodes, newest first).
        Use this for questions like "what did I watch last?"."""
        return await current_service().history(type, limit)

    @server.tool(title="Up next", annotations=read_only)
    @trakt_errors
    async def get_up_next(
        limit: Annotated[int, Field(ge=1, le=50)] = 10,
    ) -> UpNext:
        """Shows the user is in the middle of (not finished or dropped), each with remaining
        episodes and the next episode to watch. Not a watch history - use `get_history`
        for what was watched recently."""
        return await current_service().up_next(limit)

    @server.tool(
        title="Mark as watched",
        annotations=ToolAnnotations(
            readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True
        ),
    )
    @trakt_errors
    async def mark_watched(
        movie_trakt_ids: Annotated[list[int], Field(max_length=MAX_ITEMS_PER_CALL)] = [],  # noqa: B006
        episodes: Annotated[list[EpisodeWatch], Field(max_length=MAX_ITEMS_PER_CALL)] = [],  # noqa: B006
        watched_at: Annotated[
            datetime | None, Field(description="When it was watched (ISO 8601); default now.")
        ] = None,
    ) -> HistoryChange:
        """Record that the user watched movies and/or specific episodes. Each call adds a
        new play, so do not repeat it for the same viewing."""
        if not movie_trakt_ids and not episodes:
            raise ToolError("Pass at least one movie or episode.")
        if any(mid <= 0 for mid in movie_trakt_ids):
            raise ToolError("Movie IDs must be positive Trakt IDs.")
        return await current_service().mark_watched(
            movie_trakt_ids,
            [(e.show_trakt_id, e.season, e.episode) for e in episodes],
            watched_at,
        )

    @server.tool(title="Watch statistics", annotations=read_only)
    @trakt_errors
    async def get_stats() -> WatchStats:
        """Lifetime statistics: movies and episodes watched and total watch time."""
        return await current_service().stats()


async def _describe(exc: TraktError, current_service: Callable[[], TraktService]) -> str:
    """Turn a Trakt error into a message the assistant can act on."""
    if isinstance(exc, TraktLimitError):
        limit: Any = None
        try:
            limit = await current_service().watchlist_limit()
        except TraktError:
            logger.debug("Could not fetch the watchlist limit", exc_info=True)
        size = f" of {limit} items" if limit else ""
        return (
            f"The Trakt watchlist is full (account limit{size} reached). Nothing was added. "
            "Ask the user to remove items, or check their Trakt VIP status."
        )
    if isinstance(exc, TraktRateLimitError):
        wait = f" Retry after {exc.retry_after} seconds." if exc.retry_after else ""
        return f"Trakt is rate limiting requests.{wait}"
    if isinstance(exc, TraktNotFoundError):
        return "Trakt could not find that item. Use `search` to get a valid trakt_id."
    if isinstance(exc, TraktAuthError):
        return (
            str(exc)
            if "reconnect" in str(exc)
            else ("Trakt rejected the request. Ask the user to reconnect the Trakt connector.")
        )
    return str(exc)
