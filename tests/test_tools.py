"""Tool behaviour through the real MCP endpoint, against the fake Trakt."""

import json
from typing import Any

import pytest

from tests.conftest import McpSession
from tests.fake_trakt import FakeTrakt
from trakt_mcp.server import App
from trakt_mcp.trakt.service import OVERVIEW_MAX_CHARS, escape_search_query


def ok(result: dict[str, Any]) -> dict[str, Any]:
    assert result["isError"] is False, result
    return result["structuredContent"]  # type: ignore[no-any-return]


def error_text(result: dict[str, Any]) -> str:
    assert result["isError"] is True, result
    return result["content"][0]["text"]  # type: ignore[no-any-return]


async def test_tool_list_is_lean_and_annotated(mcp: McpSession) -> None:
    tools = (await mcp.rpc("tools/list")).json()["result"]["tools"]
    names = {tool["name"]: tool for tool in tools}
    assert set(names) == {
        "search",
        "get_watchlist",
        "add_to_watchlist",
        "remove_from_watchlist",
        "get_show_progress",
        "get_up_next",
        "get_history",
        "mark_watched",
        "get_stats",
    }
    assert names["remove_from_watchlist"]["annotations"]["destructiveHint"] is True
    assert names["search"]["annotations"]["readOnlyHint"] is True
    assert all(tool.get("outputSchema") for tool in tools)


# -- search -------------------------------------------------------------------


async def test_search_returns_compact_results(mcp: McpSession) -> None:
    result = ok(await mcp.call("search", query="dune"))
    [dune] = result["results"]
    assert dune["title"] == "Dune" and dune["trakt_id"] == 2 and dune["type"] == "movie"
    assert len(dune["overview"]) == OVERVIEW_MAX_CHARS
    assert dune["overview"].endswith("…")


async def test_search_by_type_and_year(mcp: McpSession, fake_trakt: FakeTrakt) -> None:
    result = ok(await mcp.call("search", query="severance", type="show", year=2022, limit=3))
    assert [r["title"] for r in result["results"]] == ["Severance"]
    query = fake_trakt.requests[-1].url.params
    assert fake_trakt.requests[-1].url.path == "/search/show"
    assert query["years"] == "2022" and query["limit"] == "3" and query["extended"] == "full"


async def test_search_escapes_special_characters(mcp: McpSession, fake_trakt: FakeTrakt) -> None:
    await mcp.call("search", query="Mission: Impossible")
    assert fake_trakt.requests[-1].url.params["query"] == "Mission\\: Impossible"


def test_escape_search_query() -> None:
    assert escape_search_query("  a+b (c)  ") == "a\\+b \\(c\\)"


@pytest.mark.parametrize(
    "args", [{"query": ""}, {"query": "x", "limit": 50}, {"query": "x", "type": "person"}]
)
async def test_search_validates_input(mcp: McpSession, args: dict[str, Any]) -> None:
    assert (await mcp.call("search", **args))["isError"] is True


# -- watchlist ----------------------------------------------------------------


async def test_add_get_and_remove_watchlist(mcp: McpSession, fake_trakt: FakeTrakt) -> None:
    added = ok(
        await mcp.call(
            "add_to_watchlist",
            items=[{"type": "show", "trakt_id": 10}, {"type": "movie", "trakt_id": 1}],
        )
    )
    assert added == {"changed": 2, "already_present": 0, "not_found": []}

    again = ok(await mcp.call("add_to_watchlist", items=[{"type": "movie", "trakt_id": 1}]))
    assert again["changed"] == 0 and again["already_present"] == 1

    watchlist = ok(await mcp.call("get_watchlist"))
    assert watchlist["total"] == 2
    assert watchlist["account_limit"] == 100
    assert [i["title"] for i in watchlist["items"]] == [
        "Severance",
        "Arrival",
    ]  # newest first (fake adds movies first)
    assert all(i["overview"] is None for i in watchlist["items"])

    only_shows = ok(await mcp.call("get_watchlist", type="show", limit=1))
    assert [i["title"] for i in only_shows["items"]] == ["Severance"]

    removed = ok(await mcp.call("remove_from_watchlist", items=[{"type": "movie", "trakt_id": 1}]))
    assert removed["changed"] == 1
    assert [w[1] for w in fake_trakt.watchlist] == [10]


async def test_duplicates_in_one_call_are_sent_once(mcp: McpSession, fake_trakt: FakeTrakt) -> None:
    await mcp.call("add_to_watchlist", items=[{"type": "movie", "trakt_id": 1}] * 3)
    body = json.loads(fake_trakt.requests[-1].content)
    assert body == {"movies": [{"ids": {"trakt": 1}}], "shows": []}


async def test_unknown_ids_are_reported(mcp: McpSession) -> None:
    result = ok(await mcp.call("add_to_watchlist", items=[{"type": "movie", "trakt_id": 999}]))
    assert result["not_found"] == [{"type": "movie", "trakt_id": 999}]


async def test_full_watchlist_gives_a_clear_message(mcp: McpSession, fake_trakt: FakeTrakt) -> None:
    fake_trakt.watchlist_limit = 1
    ok(await mcp.call("add_to_watchlist", items=[{"type": "movie", "trakt_id": 1}]))
    text = error_text(await mcp.call("add_to_watchlist", items=[{"type": "movie", "trakt_id": 2}]))
    assert "watchlist is full" in text
    assert "1 items" in text


@pytest.mark.parametrize(
    "items", [[], [{"type": "movie", "trakt_id": 0}], [{"type": "movie", "trakt_id": 1}] * 21]
)
async def test_watchlist_input_validation(mcp: McpSession, items: list[Any]) -> None:
    assert (await mcp.call("add_to_watchlist", items=items))["isError"] is True


# -- progress -----------------------------------------------------------------


async def test_show_progress(mcp: McpSession) -> None:
    progress = ok(await mcp.call("get_show_progress", show_trakt_id=10))
    assert progress["show"]["title"] == "Severance"
    assert (
        progress["aired_episodes"],
        progress["watched_episodes"],
        progress["remaining_episodes"],
    ) == (19, 12, 7)
    assert progress["percent_complete"] == 63.2
    assert progress["next_episode"] == {"season": 2, "number": 3, "title": "Who Is Alive?"}
    assert progress["seasons"][1] == {"number": 2, "aired": 10, "watched": 3}


async def test_show_progress_unknown_show(mcp: McpSession) -> None:
    text = error_text(await mcp.call("get_show_progress", show_trakt_id=12345))
    assert "search" in text


async def test_up_next(mcp: McpSession) -> None:
    [entry] = ok(await mcp.call("get_up_next", limit=5))["shows"]
    assert entry["show"]["title"] == "Severance"
    assert entry["remaining_episodes"] == 7
    assert entry["next_episode"]["number"] == 3


# -- history & stats ------------------------------------------------------------


async def test_mark_watched(mcp: McpSession, fake_trakt: FakeTrakt) -> None:
    result = ok(
        await mcp.call(
            "mark_watched",
            movie_trakt_ids=[1, 999],
            episodes=[
                {"show_trakt_id": 10, "season": 2, "episode": 3},
                {"show_trakt_id": 10, "season": 2, "episode": 4},
            ],
            watched_at="2026-11-06T20:00:00Z",
        )
    )
    assert result == {"added_movies": 1, "added_episodes": 2, "not_found": 1}
    [body] = fake_trakt.history
    assert body["movies"][0]["watched_at"] == "2026-11-06T20:00:00+00:00"
    assert body["shows"] == [
        {
            "ids": {"trakt": 10},
            "seasons": [
                {
                    "number": 2,
                    "episodes": [
                        {"number": 3, "watched_at": "2026-11-06T20:00:00+00:00"},
                        {"number": 4, "watched_at": "2026-11-06T20:00:00+00:00"},
                    ],
                }
            ],
        }
    ]


async def test_mark_watched_without_timestamp(mcp: McpSession, fake_trakt: FakeTrakt) -> None:
    ok(await mcp.call("mark_watched", movie_trakt_ids=[1]))
    assert fake_trakt.history[0]["movies"] == [{"ids": {"trakt": 1}}]


async def test_mark_watched_needs_something(mcp: McpSession) -> None:
    assert "at least one" in error_text(await mcp.call("mark_watched"))


async def test_mark_watched_rejects_bad_ids(mcp: McpSession) -> None:
    assert "positive" in error_text(await mcp.call("mark_watched", movie_trakt_ids=[-1]))


async def test_stats(mcp: McpSession) -> None:
    assert ok(await mcp.call("get_stats")) == {
        "movies_watched": 250,
        "movie_minutes": 30000,
        "shows_watched": 40,
        "episodes_watched": 1900,
        "episode_minutes": 84000,
        "total_hours": 1900.0,
    }


# -- error mapping ------------------------------------------------------------


async def test_rate_limit_message(mcp: McpSession, fake_trakt: FakeTrakt) -> None:
    fake_trakt.fail_next = 429
    assert "Retry after 30 seconds" in error_text(await mcp.call("get_stats"))


async def test_unavailable_message(mcp: McpSession, fake_trakt: FakeTrakt) -> None:
    fake_trakt.fail_next = 503
    assert "temporarily unavailable" in error_text(await mcp.call("get_stats"))


async def test_revoked_trakt_access_asks_to_reconnect(
    app: App, mcp: McpSession, fake_trakt: FakeTrakt
) -> None:
    fake_trakt.access_tokens.clear()
    fake_trakt.refresh_tokens.clear()
    text = error_text(await mcp.call("get_stats"))
    assert "reconnect" in text
    assert await app.store.get_trakt_tokens("alice") is None


async def test_expired_trakt_token_is_refreshed_transparently(
    mcp: McpSession, fake_trakt: FakeTrakt
) -> None:
    fake_trakt.access_tokens.clear()  # access token revoked, refresh token still valid
    ok(await mcp.call("get_stats"))
    assert fake_trakt.refresh_calls == 1


async def test_history_lists_recent_watches_newest_first(
    mcp: McpSession, fake_trakt: FakeTrakt
) -> None:
    result = ok(await mcp.call("get_history", limit=5))
    assert [i["type"] for i in result["items"]] == ["episode", "movie"]
    episode, movie = result["items"]
    assert episode["title"] == "Severance" and episode["episode"]["number"] == 3
    assert movie == {
        "watched_at": "2026-11-01T20:00:00.000Z",
        "type": "movie",
        "title": "Arrival",
        "year": 2016,
        "trakt_id": 1,
        "episode": None,
    }
    assert fake_trakt.requests[-1].url.path == "/sync/history"
    assert fake_trakt.requests[-1].url.params["limit"] == "5"


async def test_history_filtered_by_type(mcp: McpSession, fake_trakt: FakeTrakt) -> None:
    result = ok(await mcp.call("get_history", type="movies"))
    assert [i["title"] for i in result["items"]] == ["Arrival"]
    assert fake_trakt.requests[-1].url.path == "/sync/history/movies"


async def test_up_next_points_to_history_in_its_description(mcp: McpSession) -> None:
    tools = (await mcp.rpc("tools/list")).json()["result"]["tools"]
    up_next = next(t for t in tools if t["name"] == "get_up_next")
    assert "get_history" in up_next["description"]
