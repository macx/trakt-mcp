"""The Trakt operations behind our MCP tools, for one signed-in user.

This layer knows Trakt's request and response shapes and converts them into
the compact models in :mod:`trakt_mcp.models`. Trakt responses are parsed
defensively: unknown fields are ignored and missing optional fields become
``None`` rather than errors.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, Literal

from trakt_mcp.models import (
    EpisodeRef,
    History,
    HistoryChange,
    HistoryEntry,
    HistoryRemoval,
    ListChange,
    MediaItem,
    MediaRef,
    MediaSummary,
    MediaType,
    PlaybackEntry,
    PlaybackProgress,
    PlaybackRemoval,
    SearchResult,
    SeasonProgress,
    ShowProgress,
    UpNext,
    UpNextEntry,
    WatchedItems,
    Watchlist,
    WatchlistEntry,
    WatchStats,
)
from trakt_mcp.trakt.errors import (
    TraktError,
    TraktNotFoundError,
    TraktRateLimitError,
    TraktUnavailableError,
)
from trakt_mcp.trakt.http import TraktApi
from trakt_mcp.trakt.sessions import TraktSessions

logger = logging.getLogger(__name__)

OVERVIEW_MAX_CHARS = 200
_PLURAL: dict[MediaType, str] = {"movie": "movies", "show": "shows"}
# Characters Trakt's search engine treats specially; they are escaped so a
# title like "Mission: Impossible" is searched literally.
_SEARCH_SPECIAL = set('+-&|!(){}[]^"~*?:/\\')


class TraktService:
    def __init__(self, api: TraktApi, sessions: TraktSessions, subject: str) -> None:
        self._api = api
        self._sessions = sessions
        self._subject = subject

    async def _call(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
    ) -> Any:
        def send(token: str) -> Awaitable[Any]:
            return self._api.request(method, path, access_token=token, params=params, json=json)

        return await self._sessions.call(self._subject, send)

    # -- search -------------------------------------------------------------

    async def search(
        self, query: str, media_type: MediaType | None, year: int | None, limit: int
    ) -> SearchResult:
        types = media_type or "movie,show"
        params: dict[str, Any] = {
            "query": escape_search_query(query),
            "extended": "full",
            "limit": limit,
        }
        if year is not None:
            params["years"] = year
        data = await self._call("GET", f"/search/{types}", params=params)

        def parse(entry: dict[str, Any]) -> MediaItem | None:
            media = entry.get("type")
            if media not in _PLURAL or not isinstance(entry.get(media), dict):
                return None
            return _media_item(media, entry[media])

        return SearchResult(results=_parse_each(data, parse)[:limit])

    # -- watchlist ----------------------------------------------------------

    async def watchlist(self, media_type: MediaType | None, limit: int) -> Watchlist:
        types: list[MediaType] = [media_type] if media_type else ["show", "movie"]
        responses = await asyncio.gather(
            *(self._call("GET", f"/sync/watchlist/{_PLURAL[t]}/added/desc") for t in types),
            self._call("GET", "/users/settings"),
        )
        *lists, settings = responses

        def parser(media: MediaType) -> Callable[[dict[str, Any]], WatchlistEntry | None]:
            def parse(entry: dict[str, Any]) -> WatchlistEntry | None:
                payload = entry.get(media)
                if not isinstance(payload, dict):
                    return None
                item = _summary(media, payload)
                return WatchlistEntry(**item.model_dump(), listed_at=entry.get("listed_at"))

            return parse

        entries: list[WatchlistEntry] = []
        for media, raw in zip(types, lists, strict=True):
            entries += _parse_each(raw, parser(media))
        entries.sort(key=lambda e: e.listed_at or "", reverse=True)
        return Watchlist(
            items=entries[:limit],
            total=len(entries),
            account_limit=_watchlist_limit(settings),
        )

    async def watchlist_limit(self) -> int | None:
        return _watchlist_limit(await self._call("GET", "/users/settings"))

    async def add_to_watchlist(self, items: list[MediaRef]) -> ListChange:
        data = await self._call("POST", "/sync/watchlist", json=_media_body(items))
        return ListChange(
            changed=_count(data, "added"),
            already_present=_count(data, "existing"),
            not_found=_not_found(data),
        )

    async def remove_from_watchlist(self, items: list[MediaRef]) -> ListChange:
        data = await self._call("POST", "/sync/watchlist/remove", json=_media_body(items))
        return ListChange(changed=_count(data, "deleted"), not_found=_not_found(data))

    # -- progress & stats ---------------------------------------------------

    async def show_progress(self, show_id: int) -> ShowProgress:
        show, progress = await asyncio.gather(
            self._call("GET", f"/shows/{show_id}"),
            self._call("GET", f"/shows/{show_id}/progress/watched"),
        )
        aired, watched = int(progress.get("aired", 0)), int(progress.get("completed", 0))
        return ShowProgress(
            show=_summary("show", show),
            aired_episodes=aired,
            watched_episodes=watched,
            remaining_episodes=max(aired - watched, 0),
            percent_complete=round(100 * watched / aired, 1) if aired else 0.0,
            last_watched_at=progress.get("last_watched_at"),
            next_episode=_episode(progress.get("next_episode")),
            seasons=[
                SeasonProgress(
                    number=int(season["number"]),
                    aired=int(season.get("aired", 0)),
                    watched=int(season.get("completed", 0)),
                )
                for season in progress.get("seasons") or []
            ],
        )

    async def history(self, kind: Literal["movies", "episodes"] | None, limit: int) -> History:
        path = f"/sync/history/{kind}" if kind else "/sync/history"
        data = await self._call("GET", path, params={"page": 1, "limit": limit})

        def parse(entry: dict[str, Any]) -> HistoryEntry | None:
            media, episode = _watched_media(entry)
            if media is None:
                return None
            return HistoryEntry(
                watched_at=str(entry.get("watched_at") or ""),
                type=entry["type"],
                title=media.title,
                year=media.year,
                trakt_id=media.trakt_id,
                episode=episode,
            )

        return History(items=_parse_each(data, parse)[:limit])

    async def up_next(self, limit: int) -> UpNext:
        data = await self._call("GET", "/sync/progress/up_next", params={"page": 1, "limit": limit})

        def parse(entry: dict[str, Any]) -> UpNextEntry | None:
            if not isinstance(entry.get("show"), dict):
                return None
            progress = entry.get("progress") or {}
            aired, watched = int(progress.get("aired", 0)), int(progress.get("completed", 0))
            return UpNextEntry(
                show=_summary("show", entry["show"]),
                watched_episodes=watched,
                aired_episodes=aired,
                remaining_episodes=max(aired - watched, 0),
                next_episode=_episode(progress.get("next_episode")),
                last_watched_at=progress.get("last_watched_at"),
            )

        return UpNext(shows=_parse_each(data, parse)[:limit])

    async def playback(
        self, kind: Literal["movies", "episodes"] | None, limit: int
    ) -> PlaybackProgress:
        """Paused, unfinished playbacks (what players show as "Continue watching")."""
        path = f"/sync/playback/{kind}" if kind else "/sync/playback"
        data = await self._call("GET", path, params={"page": 1, "limit": limit})

        def parse(entry: dict[str, Any]) -> PlaybackEntry | None:
            playback_id = entry.get("id")
            media, episode = _watched_media(entry)
            if media is None or not isinstance(playback_id, int) or isinstance(playback_id, bool):
                return None
            return PlaybackEntry(
                playback_id=playback_id,
                progress_percent=round(float(entry.get("progress") or 0), 1),
                paused_at=str(entry.get("paused_at") or ""),
                type=entry["type"],
                title=media.title,
                year=media.year,
                trakt_id=media.trakt_id,
                episode=episode,
            )

        return PlaybackProgress(items=_parse_each(data, parse)[:limit])

    async def remove_playback(self, playback_ids: list[int]) -> PlaybackRemoval:
        """Delete paused playbacks one by one; unknown IDs are reported, not fatal.

        If Trakt rate-limits or fails after some deletions, the partial outcome is
        returned (with the IDs still to do) instead of an error that would hide
        what already changed. A failure before anything changed is raised as usual.
        """
        ids = list(dict.fromkeys(playback_ids))
        removed, not_found = 0, []
        for index, playback_id in enumerate(ids):
            try:
                await self._call("DELETE", f"/sync/playback/{playback_id}")
            except TraktNotFoundError:
                not_found.append(playback_id)
            except (TraktRateLimitError, TraktUnavailableError) as exc:
                if not removed and not not_found:
                    raise
                retry_after = exc.retry_after if isinstance(exc, TraktRateLimitError) else None
                return PlaybackRemoval(
                    removed=removed,
                    not_found=not_found,
                    retry_later=ids[index:],
                    retry_after_seconds=retry_after,
                )
            else:
                removed += 1
        return PlaybackRemoval(removed=removed, not_found=not_found)

    async def mark_watched(
        self, items: WatchedItems, watched_at: datetime | str | None
    ) -> HistoryChange:
        """Add plays to the history (one per movie/episode; whole seasons/shows expand)."""
        when = trakt_datetime(watched_at) if isinstance(watched_at, datetime) else watched_at
        body = _history_body(items, when)
        data = await self._call("POST", "/sync/history", json=body) or {}
        added, updated = data.get("added") or {}, data.get("updated") or {}
        return HistoryChange(
            added_movies=int(added.get("movies", 0)),
            added_episodes=int(added.get("episodes", 0)),
            updated_movies=int(updated.get("movies", 0)),
            updated_episodes=int(updated.get("episodes", 0)),
            not_found=_count_not_found(data),
        )

    async def unmark_watched(self, items: WatchedItems) -> HistoryRemoval:
        """Remove *all* plays of the given items from the history."""
        body = _history_body(items, None)
        data = await self._call("POST", "/sync/history/remove", json=body) or {}
        deleted = data.get("deleted") or {}
        return HistoryRemoval(
            removed_movies=int(deleted.get("movies", 0)),
            removed_episodes=int(deleted.get("episodes", 0)),
            not_found=_count_not_found(data),
        )

    async def stats(self) -> WatchStats:
        data = await self._call("GET", "/users/me/stats")
        movies, shows, episodes = (data.get(k) or {} for k in ("movies", "shows", "episodes"))
        movie_minutes, episode_minutes = (
            int(movies.get("minutes", 0)),
            int(episodes.get("minutes", 0)),
        )
        return WatchStats(
            movies_watched=int(movies.get("watched", 0)),
            movie_minutes=movie_minutes,
            shows_watched=int(shows.get("watched", 0)),
            episodes_watched=int(episodes.get("watched", 0)),
            episode_minutes=episode_minutes,
            total_hours=round((movie_minutes + episode_minutes) / 60, 1),
        )


# -- conversion helpers -----------------------------------------------------


def escape_search_query(query: str) -> str:
    return "".join(f"\\{ch}" if ch in _SEARCH_SPECIAL else ch for ch in query.strip())


def _parse_each[T](entries: Any, parse: Callable[[dict[str, Any]], T | None]) -> list[T]:
    """Parse a Trakt list, skipping entries of unknown or malformed shape, so one bad
    entry cannot break the whole result.

    If Trakt returned entries but *every* one of them is malformed, the format has
    changed (or our parser regressed): that is raised, never reported as an empty
    list - an assistant would take it as "nothing there" and act on it.
    """
    parsed: list[T] = []
    failures: dict[str, int] = {}
    raw = entries if isinstance(entries, list) else []
    for entry in raw:
        try:
            item = parse(entry) if isinstance(entry, dict) else None
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            failures[type(exc).__name__] = failures.get(type(exc).__name__, 0) + 1
            continue
        if item is not None:
            parsed.append(item)
    if failures:
        logger.warning(
            "Skipped %d of %d malformed Trakt list entries: %s",
            sum(failures.values()),
            len(raw),
            ", ".join(f"{name} x{count}" for name, count in sorted(failures.items())),
        )
        if not parsed:
            raise TraktError("Trakt returned a response in an unexpected format.")
    return parsed


def _watched_media(entry: dict[str, Any]) -> tuple[MediaSummary | None, EpisodeRef | None]:
    """The movie, or the show plus episode, of a history or playback entry."""
    if entry.get("type") == "movie" and isinstance(entry.get("movie"), dict):
        return _summary("movie", entry["movie"]), None
    if entry.get("type") == "episode" and isinstance(entry.get("show"), dict):
        return _summary("show", entry["show"]), _episode(entry.get("episode"))
    return None, None


def _summary(media: MediaType, data: dict[str, Any]) -> MediaSummary:
    return MediaSummary(
        type=media,
        title=str(data.get("title") or "Untitled"),
        year=data.get("year"),
        trakt_id=int((data.get("ids") or {})["trakt"]),
    )


def _media_item(media: MediaType, data: dict[str, Any]) -> MediaItem:
    ids = data.get("ids") or {}
    overview = data.get("overview")
    if overview and len(overview) > OVERVIEW_MAX_CHARS:
        overview = overview[: OVERVIEW_MAX_CHARS - 1].rstrip() + "…"
    return MediaItem(
        type=media,
        title=str(data.get("title") or "Untitled"),
        year=data.get("year"),
        trakt_id=int(ids["trakt"]),
        slug=ids.get("slug"),
        imdb_id=ids.get("imdb"),
        overview=overview or None,
    )


def _episode(data: Any) -> EpisodeRef | None:
    if not isinstance(data, dict):
        return None
    return EpisodeRef(
        season=int(data["season"]), number=int(data["number"]), title=data.get("title")
    )


def trakt_datetime(value: datetime) -> str:
    """Trakt expects UTC timestamps with a ``Z`` suffix (``2026-11-07T20:00:00.000Z``)."""
    if value.tzinfo is None:
        raise ValueError("watched_at needs a timezone")
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _history_body(items: WatchedItems, when: str | None) -> dict[str, Any]:
    """Build a /sync/history request body. A whole show covers its seasons and
    episodes, and a whole season covers its episodes, so those are not repeated."""

    def stamped(obj: dict[str, Any]) -> dict[str, Any]:
        return {**obj, "watched_at": when} if when else obj

    whole_shows = set(items.show_trakt_ids)
    whole_seasons: dict[int, set[int]] = {}
    for sel in items.seasons:
        if sel.show_trakt_id not in whole_shows:
            whole_seasons.setdefault(sel.show_trakt_id, set()).add(sel.season)
    episodes: dict[int, dict[int, set[int]]] = {}
    for ep in items.episodes:
        if ep.show_trakt_id in whole_shows or ep.season in whole_seasons.get(ep.show_trakt_id, ()):
            continue
        episodes.setdefault(ep.show_trakt_id, {}).setdefault(ep.season, set()).add(ep.episode)

    shows: list[dict[str, Any]] = [
        stamped({"ids": {"trakt": show_id}}) for show_id in sorted(whole_shows)
    ]
    for show_id in sorted(set(whole_seasons) | set(episodes)):
        seasons = [stamped({"number": n}) for n in sorted(whole_seasons.get(show_id, ()))]
        seasons += [
            {"number": n, "episodes": [stamped({"number": e}) for e in sorted(eps)]}
            for n, eps in sorted(episodes.get(show_id, {}).items())
        ]
        shows.append({"ids": {"trakt": show_id}, "seasons": seasons})
    return {
        "movies": [
            stamped({"ids": {"trakt": mid}}) for mid in dict.fromkeys(items.movie_trakt_ids)
        ],
        "shows": shows,
    }


def _count_not_found(data: Any) -> int:
    not_found = (data or {}).get("not_found") or {}
    return sum(len(v) for v in not_found.values() if isinstance(v, list))


def _media_body(items: list[MediaRef]) -> dict[str, list[dict[str, Any]]]:
    body: dict[str, list[dict[str, Any]]] = {"movies": [], "shows": []}
    seen: set[tuple[str, int]] = set()
    for item in items:
        if (item.type, item.trakt_id) not in seen:
            seen.add((item.type, item.trakt_id))
            body[_PLURAL[item.type]].append({"ids": {"trakt": item.trakt_id}})
    return body


def _count(data: Any, key: str) -> int:
    counts = (data or {}).get(key) or {}
    return int(counts.get("movies", 0)) + int(counts.get("shows", 0))


def _not_found(data: Any) -> list[MediaRef]:
    missing = (data or {}).get("not_found") or {}
    refs: list[MediaRef] = []
    for media, plural in _PLURAL.items():
        for entry in missing.get(plural) or []:
            trakt_id = (entry.get("ids") or {}).get("trakt")
            if isinstance(trakt_id, int) and trakt_id > 0:
                refs.append(MediaRef(type=media, trakt_id=trakt_id))
    return refs


def _watchlist_limit(settings: Any) -> int | None:
    try:
        return int(settings["limits"]["watchlist"]["item_count"])
    except (KeyError, TypeError, ValueError):
        return None


ServiceFactory = Callable[[str], TraktService]
