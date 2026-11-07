"""Tool input and output models.

Outputs are intentionally compact: an assistant needs titles, years and Trakt
IDs to act, not the full Trakt payload. Every model becomes part of the tool's
JSON schema, so field descriptions are written for the model reading them.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

MediaType = Literal["movie", "show"]


class MediaRef(BaseModel):
    """Identifies a movie or show by its Trakt ID (as returned by `search`)."""

    type: MediaType
    trakt_id: int = Field(gt=0, description="Numeric Trakt ID from a search result.")


class EpisodeRef(BaseModel):
    season: int = Field(ge=0, description="Season number (0 = specials).")
    number: int = Field(ge=1, description="Episode number within the season.")
    title: str | None = None


class MediaItem(BaseModel):
    type: MediaType
    title: str
    year: int | None = None
    trakt_id: int
    slug: str | None = None
    imdb_id: str | None = None
    overview: str | None = Field(default=None, description="Shortened plot summary.")


class SearchResult(BaseModel):
    results: list[MediaItem]


class WatchlistEntry(MediaItem):
    listed_at: str | None = Field(default=None, description="When it was added (UTC).")


class Watchlist(BaseModel):
    items: list[WatchlistEntry]
    total: int = Field(description="Number of entries on the watchlist (before `limit`).")
    account_limit: int | None = Field(
        default=None, description="Maximum watchlist size of this Trakt account, if known."
    )


class ListChange(BaseModel):
    """Outcome of adding to or removing from the watchlist."""

    changed: int = Field(description="Items actually added (or removed).")
    already_present: int = Field(default=0, description="Items that were already on the list.")
    not_found: list[MediaRef] = Field(default_factory=list)


class SeasonProgress(BaseModel):
    number: int
    aired: int
    watched: int


class ShowProgress(BaseModel):
    show: MediaItem
    aired_episodes: int = Field(description="Episodes aired so far (specials excluded).")
    watched_episodes: int
    remaining_episodes: int
    percent_complete: float
    last_watched_at: str | None = None
    next_episode: EpisodeRef | None = Field(
        default=None, description="Next episode to watch; null if fully caught up."
    )
    seasons: list[SeasonProgress]


class UpNextEntry(BaseModel):
    show: MediaItem
    watched_episodes: int
    aired_episodes: int
    remaining_episodes: int
    next_episode: EpisodeRef | None = None
    last_watched_at: str | None = None


class UpNext(BaseModel):
    shows: list[UpNextEntry]


class HistoryEntry(BaseModel):
    watched_at: str = Field(description="When it was watched (UTC, ISO 8601).")
    type: Literal["movie", "episode"]
    title: str = Field(description="Movie title, or the show title for episodes.")
    year: int | None = None
    trakt_id: int = Field(description="Trakt ID of the movie or show.")
    episode: EpisodeRef | None = Field(default=None, description="Set for episodes.")


class History(BaseModel):
    items: list[HistoryEntry] = Field(description="Most recent first.")


class EpisodeSelector(BaseModel):
    show_trakt_id: int = Field(gt=0, description="Trakt ID of the show.")
    season: int = Field(ge=0, description="Season number (0 = specials).")
    episode: int = Field(ge=1, description="Episode number within the season.")


class SeasonSelector(BaseModel):
    show_trakt_id: int = Field(gt=0, description="Trakt ID of the show.")
    season: int = Field(ge=0, description="Season number (0 = specials).")


class WatchedItems(BaseModel):
    """What to mark or unmark: any mix of movies, episodes, whole seasons, whole shows."""

    movie_trakt_ids: list[int] = Field(default_factory=list)
    episodes: list[EpisodeSelector] = Field(default_factory=list)
    seasons: list[SeasonSelector] = Field(default_factory=list)
    show_trakt_ids: list[int] = Field(default_factory=list)

    def is_empty(self) -> bool:
        return not (self.movie_trakt_ids or self.episodes or self.seasons or self.show_trakt_ids)


class HistoryChange(BaseModel):
    added_movies: int
    added_episodes: int
    updated_movies: int = Field(default=0, description="Existing plays Trakt updated.")
    updated_episodes: int = 0
    not_found: int = Field(description="Number of requested items Trakt could not match.")


class HistoryRemoval(BaseModel):
    removed_movies: int = Field(description="Movie plays removed from the history.")
    removed_episodes: int = Field(description="Episode plays removed from the history.")
    not_found: int = Field(description="Number of requested items Trakt could not match.")


class WatchStats(BaseModel):
    movies_watched: int
    movie_minutes: int
    shows_watched: int
    episodes_watched: int
    episode_minutes: int
    total_hours: float
