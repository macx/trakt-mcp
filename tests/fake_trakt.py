"""An in-memory stand-in for Trakt's OAuth host and API, used as an httpx transport.

It implements just enough behaviour (and validation) for the endpoints we
call, so tests exercise real request/response round trips instead of mocks of
our own functions.
"""

from __future__ import annotations

import json
import secrets
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx

CLIENT_ID = "test-client-id"
MOVIES: dict[int, dict[str, Any]] = {
    1: {
        "title": "Arrival",
        "year": 2016,
        "ids": {"trakt": 1, "slug": "arrival-2016", "imdb": "tt2543164"},
        "overview": "A linguist works with the military to communicate with alien lifeforms.",
    },
    2: {
        "title": "Dune",
        "year": 2021,
        "ids": {"trakt": 2, "slug": "dune-2021"},
        "overview": "x" * 500,
    },
}
SHOWS: dict[int, dict[str, Any]] = {
    10: {
        "title": "Severance",
        "year": 2022,
        "ids": {"trakt": 10, "slug": "severance"},
        "overview": "Office workers have their memories surgically divided.",
    },
}


@dataclass
class FakeTrakt:
    username: str = "alice"
    watchlist_limit: int = 100
    access_ttl: int = 86400
    watchlist: list[tuple[str, int, str]] = field(default_factory=list)
    history: list[dict[str, Any]] = field(default_factory=list)
    requests: list[httpx.Request] = field(default_factory=list)
    # device_code -> [user_code, status] with status pending|approved|denied|used
    devices: dict[str, list[str]] = field(default_factory=dict)
    device_interval: int = 5
    access_tokens: set[str] = field(default_factory=set)
    refresh_tokens: set[str] = field(default_factory=set)
    refresh_calls: int = 0
    revoked: int = 0
    fail_next: int | None = None  # force the next API call to return this status

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    # -- helpers for tests --------------------------------------------------

    def approve_device(self, status: str = "approved") -> None:
        """Simulate the user entering the latest code at trakt.tv/activate."""
        device = list(self.devices.values())[-1]
        device[1] = status

    def _tokens(self) -> dict[str, Any]:
        access, refresh = secrets.token_hex(8), secrets.token_hex(8)
        self.access_tokens.add(access)
        self.refresh_tokens.add(refresh)
        return {
            "access_token": access,
            "refresh_token": refresh,
            "token_type": "bearer",
            "expires_in": self.access_ttl,
            "scope": "public",
            "created_at": int(time.time()),
        }

    # -- transport ----------------------------------------------------------

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = urlsplit(str(request.url))
        if url.hostname == "auth.trakt.tv":
            return self._auth(request, url.path)
        if url.hostname == "api.trakt.tv":
            if request.headers.get("trakt-api-key") != CLIENT_ID:
                return httpx.Response(403)
            if request.headers.get("trakt-api-version") != "2":
                return httpx.Response(400)
            token = request.headers.get("authorization", "").removeprefix("Bearer ")
            if token not in self.access_tokens:
                return httpx.Response(401)
            if self.fail_next is not None:
                status, self.fail_next = self.fail_next, None
                return httpx.Response(status, headers={"Retry-After": "30"})
            return self._api(request, url.path, parse_qs(url.query))
        return httpx.Response(404)

    def _auth(self, request: httpx.Request, path: str) -> httpx.Response:
        if path == "/oauth/revoke" and request.method == "POST":
            self.access_tokens.discard(json.loads(request.content).get("token", ""))
            self.revoked += 1
            return httpx.Response(200)
        if request.method != "POST":
            return httpx.Response(404)
        body = json.loads(request.content)
        if body.get("client_id") != CLIENT_ID:
            return httpx.Response(401, json={"error": "invalid_client"})
        if path == "/oauth/device/code":
            device_code = secrets.token_hex(16)
            user_code = secrets.token_hex(4).upper()
            self.devices[device_code] = [user_code, "pending"]
            return httpx.Response(
                200,
                json={
                    "device_code": device_code,
                    "user_code": user_code,
                    "verification_url": "https://trakt.tv/activate",
                    "expires_in": 600,
                    "interval": self.device_interval,
                },
            )
        if path == "/oauth/device/token":
            device = self.devices.get(body.get("code", ""))
            if device is None:
                return httpx.Response(404)
            status_codes = {
                "pending": 400,
                "denied": 418,
                "used": 409,
                "expired": 410,
                "slow_down": 429,
            }
            if device[1] in status_codes:
                return httpx.Response(status_codes[device[1]])
            device[1] = "used"
            return httpx.Response(200, json=self._tokens())
        if path != "/oauth/token":
            return httpx.Response(404)
        if body["grant_type"] == "refresh_token":
            self.refresh_calls += 1
            if body.get("refresh_token") not in self.refresh_tokens:
                return httpx.Response(400, json={"error": "invalid_grant"})
            self.refresh_tokens.discard(body["refresh_token"])  # single use
            return httpx.Response(200, json=self._tokens())
        return httpx.Response(400, json={"error": "unsupported_grant_type"})

    def _api(
        self, request: httpx.Request, path: str, query: dict[str, list[str]]
    ) -> httpx.Response:
        method = request.method
        if method == "GET" and path == "/users/settings":
            return httpx.Response(
                200,
                json={
                    "user": {"username": self.username.title(), "ids": {"slug": self.username}},
                    "limits": {
                        "list": {"count": 5, "item_count": 100},
                        "watchlist": {"item_count": self.watchlist_limit},
                        "favorites": {"item_count": 50},
                    },
                },
            )
        if method == "GET" and path.startswith("/search/"):
            types = path.removeprefix("/search/").split(",")
            needle = query.get("query", [""])[0].replace("\\", "").lower()
            results = [
                {"type": "movie", "score": 1, "movie": m}
                for m in MOVIES.values()
                if "movie" in types and needle in m["title"].lower()
            ]
            results += [
                {"type": "show", "score": 1, "show": s}
                for s in SHOWS.values()
                if "show" in types and needle in s["title"].lower()
            ]
            return httpx.Response(200, json=results)
        if method == "GET" and path.startswith("/sync/watchlist/"):
            plural = path.split("/")[3]
            media = plural.removesuffix("s")
            catalogue = MOVIES if media == "movie" else SHOWS
            items = [
                {"rank": i, "id": i, "listed_at": at, "type": media, media: catalogue[tid]}
                for i, (m, tid, at) in enumerate(self.watchlist)
                if m == media
            ]
            return httpx.Response(200, json=items)
        if method == "POST" and path in ("/sync/watchlist", "/sync/watchlist/remove"):
            return self._change_watchlist(json.loads(request.content), path.endswith("remove"))
        if method == "GET" and path == "/shows/10":
            return httpx.Response(200, json=SHOWS[10])
        if method == "GET" and path == "/shows/10/progress/watched":
            return httpx.Response(
                200,
                json={
                    "aired": 19,
                    "completed": 12,
                    "last_watched_at": "2026-10-01T20:00:00.000Z",
                    "reset_at": None,
                    "next_episode": {
                        "season": 2,
                        "number": 3,
                        "title": "Who Is Alive?",
                        "ids": {"trakt": 99},
                    },
                    "last_episode": None,
                    "seasons": [
                        {"number": 1, "aired": 9, "completed": 9, "episodes": []},
                        {"number": 2, "aired": 10, "completed": 3, "episodes": []},
                    ],
                    "hidden_seasons": [],
                },
            )
        if method == "GET" and path.startswith("/shows/"):
            return httpx.Response(404)
        if method == "GET" and path == "/sync/progress/up_next":
            return httpx.Response(
                200,
                json=[
                    {
                        "show": SHOWS[10],
                        "progress": {
                            "aired": 19,
                            "completed": 12,
                            "reset_at": None,
                            "next_episode": {"season": 2, "number": 3, "title": "Who Is Alive?"},
                            "last_episode": None,
                            "last_watched_at": None,
                        },
                    }
                ],
            )
        if method == "GET" and path.startswith("/sync/history"):
            kind = path.removeprefix("/sync/history").strip("/")
            entries = [
                {
                    "id": 2,
                    "watched_at": "2026-11-06T21:10:00.000Z",
                    "action": "watch",
                    "type": "episode",
                    "episode": {"season": 2, "number": 3, "title": "Who Is Alive?"},
                    "show": SHOWS[10],
                },
                {
                    "id": 1,
                    "watched_at": "2026-11-01T20:00:00.000Z",
                    "action": "watch",
                    "type": "movie",
                    "movie": MOVIES[1],
                },
                {
                    "id": 0,
                    "watched_at": "2026-10-01T20:00:00.000Z",
                    "action": "watch",
                    "type": "season",
                },  # unknown shapes are skipped
            ]
            if kind:
                entries = [e for e in entries if f"{e['type']}s" == kind]
            limit = int(query.get("limit", ["10"])[0])
            return httpx.Response(200, json=entries[:limit])
        if method == "POST" and path == "/sync/history":
            body = json.loads(request.content)
            self.history.append(body)
            episodes = sum(
                len(s["episodes"]) for show in body.get("shows", []) for s in show["seasons"]
            )
            known_movies = [m for m in body.get("movies", []) if m["ids"]["trakt"] in MOVIES]
            missing = [m for m in body.get("movies", []) if m["ids"]["trakt"] not in MOVIES]
            return httpx.Response(
                201,
                json={
                    "added": {"movies": len(known_movies), "episodes": episodes},
                    "updated": {"movies": 0, "episodes": 0},
                    "not_found": {"movies": missing, "shows": [], "seasons": [], "episodes": []},
                },
            )
        if method == "GET" and path == "/users/me/stats":
            return httpx.Response(
                200,
                json={
                    "movies": {"plays": 300, "watched": 250, "minutes": 30000},
                    "shows": {"watched": 40},
                    "episodes": {"plays": 2000, "watched": 1900, "minutes": 84000},
                },
            )
        return httpx.Response(404)

    def _change_watchlist(self, body: dict[str, Any], remove: bool) -> httpx.Response:
        counts = {
            "added" if not remove else "deleted": {"movies": 0, "shows": 0},
            "existing": {"movies": 0, "shows": 0},
        }
        not_found: dict[str, list[Any]] = {"movies": [], "shows": []}
        for plural, catalogue in (("movies", MOVIES), ("shows", SHOWS)):
            media = plural.removesuffix("s")
            for entry in body.get(plural, []):
                tid = entry["ids"]["trakt"]
                present = [w for w in self.watchlist if w[:2] == (media, tid)]
                if tid not in catalogue:
                    not_found[plural].append(entry)
                elif remove and present:
                    self.watchlist.remove(present[0])
                    counts["deleted"][plural] += 1
                elif not remove and present:
                    counts["existing"][plural] += 1
                elif not remove:
                    if len(self.watchlist) >= self.watchlist_limit:
                        return httpx.Response(420)
                    self.watchlist.append((media, tid, f"2026-11-0{len(self.watchlist) + 1}"))
                    counts["added"][plural] += 1
        status = 200 if remove else 201
        return httpx.Response(
            status,
            json={**counts, "not_found": not_found, "list": {"updated_at": "x", "item_count": 0}},
        )
