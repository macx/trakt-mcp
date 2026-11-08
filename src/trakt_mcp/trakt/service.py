"""The Trakt operations behind our MCP tools, for one signed-in user.

This layer knows Trakt's request and response shapes and converts them into
the compact models in :mod:`trakt_mcp.models`. Trakt responses are parsed
defensively: unknown fields are ignored and missing optional fields become
``None`` rather than errors.
"""

from __future__ import annotations

import asyncio
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
from trakt_mcp.trakt.errors import TraktNotFoundError
from trakt_mcp.trakt.http import TraktApi
from trakt_mcp.trakt.sessions import TraktSessions

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
        results = [
            _media_item(entry["type"], entry[entry["type"]])
            for entry in data or []
            if entry.get("type") in _PLURAL and isinstance(entry.get(entry["type"]), dict)
        ]
        return SearchResult(results=results[:limit])

    # -- watchlist ----------------------------------------------------------

    async def watchlist(self, media_type: MediaType | None, limit: int) -> Watchlist:
        types: list[MediaType] = [media_type] if media_type else ["show", "movie"]
        responses = await asyncio.gather(
            *(self._call("GET", f"/sync/watchlist/{_PLURAL[t]}/added/desc") for t in types),
            self._call("GET", "/users/settings"),
        )
        *lists, settings = responses
        entries: list[WatchlistEntry] = []
        for media, raw in zip(types, lists, strict=True):
            for entry in raw or []:
                payload = entry.get(media)
                if isinstance(payload, dict):
                    item = _media_item(media, payload, with_overview=False)
                    entries.append(
                        WatchlistEntry(**item.model_dump(), listed_at=entry.get("listed_at"))
                    )
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
            self._call("GET", f"/shows/{show_id}", params={"extended": "full"}),
            self._call("GET", f"/shows/{show_id}/progress/watched"),
        )
        aired, watched = int(progress.get("aired", 0)), int(progress.get("completed", 0))
        return ShowProgress(
            show=_media_item("show", show),
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
        items: list[HistoryEntry] = []
        for entry in data or []:
            kind_ = entry.get("type")
            if kind_ == "movie" and isinstance(entry.get("movie"), dict):
                movie = _media_item("movie", entry["movie"], with_overview=False)
                items.append(
                    HistoryEntry(
                        watched_at=str(entry.get("watched_at", "")),
                        type="movie",
                        title=movie.title,
                        year=movie.year,
                        trakt_id=movie.trakt_id,
                    )
                )
            elif kind_ == "episode" and isinstance(entry.get("show"), dict):
                show = _media_item("show", entry["show"], with_overview=False)
                items.append(
                    HistoryEntry(
                        watched_at=str(entry.get("watched_at", "")),
                        type="episode",
                        title=show.title,
                        year=show.year,
                        trakt_id=show.trakt_id,
                        episode=_episode(entry.get("episode")),
                    )
                )
        return History(items=items[:limit])

    async def up_next(self, limit: int) -> UpNext:
        data = await self._call("GET", "/sync/progress/up_next", params={"page": 1, "limit": limit})
        shows: list[UpNextEntry] = []
        for entry in data or []:
            progress = entry.get("progress") or {}
            aired, watched = int(progress.get("aired", 0)), int(progress.get("completed", 0))
            shows.append(
                UpNextEntry(
                    show=_media_item("show", entry["show"], with_overview=False),
                    watched_episodes=watched,
                    aired_episodes=aired,
                    remaining_episodes=max(aired - watched, 0),
                    next_episode=_episode(progress.get("next_episode")),
                    last_watched_at=progress.get("last_watched_at"),
                )
            )
        return UpNext(shows=shows[:limit])

    async def playback(
        self, kind: Literal["movies", "episodes"] | None, limit: int
    ) -> PlaybackProgress:
        """Paused, unfinished playbacks (what players show as "Continue watching")."""
        path = f"/sync/playback/{kind}" if kind else "/sync/playback"
        data = await self._call("GET", path, params={"page": 1, "limit": limit})
        items: list[PlaybackEntry] = []
        for entry in data or []:
            kind_, playback_id = entry.get("type"), entry.get("id")
            if not isinstance(playback_id, int):
                continue
            if kind_ == "movie" and isinstance(entry.get("movie"), dict):
                media = _media_item("movie", entry["movie"], with_overview=False)
                episode = None
            elif kind_ == "episode" and isinstance(entry.get("show"), dict):
                media = _media_item("show", entry["show"], with_overview=False)
                episode = _episode(entry.get("episode"))
            else:
                continue
            items.append(
                PlaybackEntry(
                    playback_id=playback_id,
                    progress_percent=round(float(entry.get("progress") or 0), 1),
                    paused_at=str(entry.get("paused_at", "")),
                    type=kind_,
                    title=media.title,
                    year=media.year,
                    trakt_id=media.trakt_id,
                    episode=episode,
                )
            )
        return PlaybackProgress(items=items[:limit])

    async def remove_playback(self, playback_ids: list[int]) -> PlaybackRemoval:
        """Delete paused playbacks one by one; unknown IDs are reported, not fatal."""
        removed, not_found = 0, []
        for playback_id in dict.fromkeys(playback_ids):
            try:
                await self._call("DELETE", f"/sync/playback/{playback_id}")
            except TraktNotFoundError:
                not_found.append(playback_id)
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


def _media_item(media: MediaType, data: dict[str, Any], *, with_overview: bool = True) -> MediaItem:
    ids = data.get("ids") or {}
    overview = data.get("overview") if with_overview else None
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
