"""Tool behaviour through the real MCP endpoint, against the fake Trakt."""

import json
from datetime import datetime
from typing import Any

import httpx
import pytest

from tests.conftest import McpSession
from tests.fake_trakt import FakeTrakt
from trakt_mcp.server import App
from trakt_mcp.trakt.service import OVERVIEW_MAX_CHARS, escape_search_query, trakt_datetime


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
        "unmark_watched",
        "mark_watched",
        "get_stats",
        "get_playback",
        "remove_playback",
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
    assert all("overview" not in i and "slug" not in i for i in watchlist["items"])

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
    assert result == {
        "added_movies": 1,
        "added_episodes": 2,
        "updated_movies": 0,
        "updated_episodes": 0,
        "not_found": 1,
    }
    [body] = fake_trakt.history
    assert body["movies"][0]["watched_at"] == "2026-11-06T20:00:00.000Z"
    assert body["shows"] == [
        {
            "ids": {"trakt": 10},
            "seasons": [
                {
                    "number": 2,
                    "episodes": [
                        {"number": 3, "watched_at": "2026-11-06T20:00:00.000Z"},
                        {"number": 4, "watched_at": "2026-11-06T20:00:00.000Z"},
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
    # Rejected by the input schema (PositiveInt) before reaching Trakt.
    assert "greater_than" in error_text(await mcp.call("mark_watched", movie_trakt_ids=[-1]))


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


async def test_mark_whole_season_and_show_with_unknown_date(
    mcp: McpSession, fake_trakt: FakeTrakt
) -> None:
    result = ok(
        await mcp.call(
            "mark_watched", seasons=[{"show_trakt_id": 10, "season": 1}], watched_at="unknown"
        )
    )
    assert result == {
        "added_movies": 0,
        "added_episodes": 9,
        "updated_movies": 0,
        "updated_episodes": 0,
        "not_found": 0,
    }
    assert fake_trakt.history[-1]["shows"] == [
        {"ids": {"trakt": 10}, "seasons": [{"number": 1, "watched_at": "unknown"}]}
    ]

    result = ok(await mcp.call("mark_watched", show_trakt_ids=[10], watched_at="released"))
    assert result["added_episodes"] == 19
    assert fake_trakt.history[-1]["shows"] == [{"ids": {"trakt": 10}, "watched_at": "released"}]


async def test_whole_show_covers_its_seasons_and_episodes(
    mcp: McpSession, fake_trakt: FakeTrakt
) -> None:
    ok(
        await mcp.call(
            "mark_watched",
            show_trakt_ids=[10],
            seasons=[{"show_trakt_id": 10, "season": 1}],
            episodes=[{"show_trakt_id": 10, "season": 2, "episode": 1}],
        )
    )
    assert fake_trakt.history[-1]["shows"] == [{"ids": {"trakt": 10}}]


async def test_whole_season_covers_its_episodes(mcp: McpSession, fake_trakt: FakeTrakt) -> None:
    ok(
        await mcp.call(
            "mark_watched",
            seasons=[{"show_trakt_id": 10, "season": 1}],
            episodes=[
                {"show_trakt_id": 10, "season": 1, "episode": 2},
                {"show_trakt_id": 10, "season": 2, "episode": 4},
            ],
        )
    )
    assert fake_trakt.history[-1]["shows"] == [
        {
            "ids": {"trakt": 10},
            "seasons": [{"number": 1}, {"number": 2, "episodes": [{"number": 4}]}],
        }
    ]


async def test_released_is_rejected_for_movies(mcp: McpSession) -> None:
    text = error_text(await mcp.call("mark_watched", movie_trakt_ids=[1], watched_at="released"))
    assert "only works for episodes" in text


async def test_invalid_watched_at_is_rejected(mcp: McpSession) -> None:
    assert (await mcp.call("mark_watched", movie_trakt_ids=[1], watched_at="yesterday"))[
        "isError"
    ] is True


async def test_mark_then_unmark_restores_the_history(
    mcp: McpSession, fake_trakt: FakeTrakt
) -> None:
    ok(await mcp.call("mark_watched", movie_trakt_ids=[1]))
    removed = ok(await mcp.call("unmark_watched", movie_trakt_ids=[1, 999]))
    assert removed == {"removed_movies": 1, "removed_episodes": 0, "not_found": 1}
    assert fake_trakt.requests[-1].url.path == "/sync/history/remove"
    assert fake_trakt.history[-1] == {
        "movies": [{"ids": {"trakt": 1}}, {"ids": {"trakt": 999}}],
        "shows": [],
    }


async def test_unmark_whole_show_and_season(mcp: McpSession, fake_trakt: FakeTrakt) -> None:
    removed = ok(await mcp.call("unmark_watched", show_trakt_ids=[10]))
    assert removed["removed_episodes"] == 19
    removed = ok(await mcp.call("unmark_watched", seasons=[{"show_trakt_id": 10, "season": 2}]))
    assert removed["removed_episodes"] == 10
    assert "watched_at" not in json.dumps(fake_trakt.history[-1])


async def test_unmark_needs_something(mcp: McpSession) -> None:
    assert "at least one" in error_text(await mcp.call("unmark_watched"))


async def test_unmark_is_marked_destructive(mcp: McpSession) -> None:
    tools = (await mcp.rpc("tools/list")).json()["result"]["tools"]
    unmark = next(t for t in tools if t["name"] == "unmark_watched")
    assert unmark["annotations"]["destructiveHint"] is True
    assert "cannot be undone" in unmark["description"]


async def test_watched_at_is_sent_as_utc_with_z(mcp: McpSession, fake_trakt: FakeTrakt) -> None:
    ok(await mcp.call("mark_watched", movie_trakt_ids=[1], watched_at="2026-11-06T21:30:00+01:00"))
    assert fake_trakt.history[-1]["movies"][0]["watched_at"] == "2026-11-06T20:30:00.000Z"


async def test_watched_at_needs_a_timezone(mcp: McpSession) -> None:
    text = error_text(
        await mcp.call("mark_watched", movie_trakt_ids=[1], watched_at="2026-11-06T21:30:00")
    )
    assert "timezone" in text


async def test_watched_at_in_the_future_is_rejected(mcp: McpSession) -> None:
    text = error_text(
        await mcp.call("mark_watched", movie_trakt_ids=[1], watched_at="2099-01-01T00:00:00Z")
    )
    assert "future" in text


async def test_unmark_refuses_whole_show_mixed_with_its_episodes(mcp: McpSession) -> None:
    text = error_text(
        await mcp.call(
            "unmark_watched",
            show_trakt_ids=[10],
            episodes=[{"show_trakt_id": 10, "season": 1, "episode": 1}],
        )
    )
    assert "entire history" in text


def test_trakt_datetime_rejects_naive_values() -> None:
    with pytest.raises(ValueError, match="timezone"):
        trakt_datetime(datetime(2026, 1, 1))


async def test_tool_calls_are_logged_with_their_arguments(
    mcp: McpSession, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("INFO", logger="trakt_mcp.tools")
    await mcp.call("mark_watched", movie_trakt_ids=[1], watched_at="unknown")
    assert any(
        r.getMessage().startswith("tool mark_watched")
        and '"watched_at": "unknown"' in r.getMessage()
        for r in caplog.records
    )


async def test_unknown_date_is_sent_verbatim(mcp: McpSession, fake_trakt: FakeTrakt) -> None:
    ok(await mcp.call("mark_watched", movie_trakt_ids=[1], watched_at="unknown"))
    assert fake_trakt.history[-1]["movies"] == [{"ids": {"trakt": 1}, "watched_at": "unknown"}]


# -- playback ("continue watching") -------------------------------------------


async def test_playback_lists_paused_items(mcp: McpSession, fake_trakt: FakeTrakt) -> None:
    items = ok(await mcp.call("get_playback"))["items"]
    assert [(i["playback_id"], i["type"], i["title"]) for i in items] == [
        (501, "episode", "Severance"),
        (500, "movie", "Arrival"),
    ]  # the malformed entry is skipped
    assert items[0]["progress_percent"] == round(61.25, 1)
    assert items[0]["episode"] == {"season": 2, "number": 3, "title": "Who Is Alive?"}
    assert items[1]["episode"] is None and items[1]["trakt_id"] == 1
    assert fake_trakt.requests[-1].url.path == "/sync/playback"


async def test_playback_filtered_by_type(mcp: McpSession, fake_trakt: FakeTrakt) -> None:
    items = ok(await mcp.call("get_playback", type="movies", limit=5))["items"]
    assert [i["title"] for i in items] == ["Arrival"]
    request = fake_trakt.requests[-1]
    assert request.url.path == "/sync/playback/movies" and request.url.params["limit"] == "5"


async def test_remove_playback_keeps_history(mcp: McpSession, fake_trakt: FakeTrakt) -> None:
    result = ok(await mcp.call("remove_playback", playback_ids=[500, 500]))
    assert result == {"removed": 1, "not_found": [], "retry_later": [], "retry_after_seconds": None}
    deletes = [r for r in fake_trakt.requests if r.method == "DELETE"]
    assert [r.url.path for r in deletes] == ["/sync/playback/500"]  # duplicates sent once
    assert fake_trakt.history == []
    remaining = ok(await mcp.call("get_playback"))["items"]
    assert [i["playback_id"] for i in remaining] == [501]


async def test_remove_playback_reports_unknown_ids(mcp: McpSession) -> None:
    result = ok(await mcp.call("remove_playback", playback_ids=[12345, 501]))
    assert result["removed"] == 1 and result["not_found"] == [12345]
    assert result["retry_later"] == []


async def test_remove_playback_surfaces_other_errors(
    mcp: McpSession, fake_trakt: FakeTrakt
) -> None:
    fake_trakt.fail_next = 503
    assert "temporarily unavailable" in error_text(
        await mcp.call("remove_playback", playback_ids=[500])
    )
    assert len(fake_trakt.playback) == 3


@pytest.mark.parametrize("ids", [[], [0], [-1], ["x"], list(range(1, 22))])
async def test_remove_playback_validates_input(mcp: McpSession, ids: list[Any]) -> None:
    assert (await mcp.call("remove_playback", playback_ids=ids))["isError"] is True


async def test_remove_playback_is_marked_destructive(mcp: McpSession) -> None:
    tools = (await mcp.rpc("tools/list")).json()["result"]["tools"]
    [tool] = [t for t in tools if t["name"] == "remove_playback"]
    assert tool["annotations"]["destructiveHint"] is True


async def test_remove_playback_reports_partial_progress_when_rate_limited(
    mcp: McpSession, fake_trakt: FakeTrakt
) -> None:
    fake_trakt.deletes_before_failure = 1
    result = ok(await mcp.call("remove_playback", playback_ids=[500, 501, 12345]))
    assert result["removed"] == 1
    assert result["retry_later"] == [501, 12345]
    assert result["retry_after_seconds"] == 2
    assert [e["id"] for e in fake_trakt.playback] == [501, 499]


async def test_remove_playback_rate_limited_before_any_change_is_an_error(
    mcp: McpSession, fake_trakt: FakeTrakt
) -> None:
    fake_trakt.deletes_before_failure = 0
    assert "rate limiting" in error_text(await mcp.call("remove_playback", playback_ids=[500]))
    assert len(fake_trakt.playback) == 3


async def test_remove_playback_partial_after_unknown_id_and_outage(
    mcp: McpSession, fake_trakt: FakeTrakt
) -> None:
    # The unknown ID counts as progress: the outage on the second ID is not raised.
    fake_trakt.deletes_before_failure, fake_trakt.delete_failure_status = 1, 503
    result = ok(await mcp.call("remove_playback", playback_ids=[12345, 500]))
    assert result == {
        "removed": 0,
        "not_found": [12345],
        "retry_later": [500],
        "retry_after_seconds": None,
    }


@pytest.mark.parametrize(
    "bad_entry",
    [
        {"id": 7, "type": "movie", "progress": 5, "paused_at": "x", "movie": {"title": "No ids"}},
        {
            "id": 7,
            "type": "movie",
            "progress": "lots",
            "paused_at": "x",
            "movie": {"ids": {"trakt": 1}},
        },
        {
            "id": True,
            "type": "movie",
            "progress": 5,
            "paused_at": "x",
            "movie": {"ids": {"trakt": 1}},
        },
        {
            "id": 7,
            "type": "episode",
            "progress": 5,
            "show": {"ids": {"trakt": 10}},
            "episode": {"season": "x"},
        },
        "not-an-object",
    ],
)
async def test_one_malformed_playback_entry_does_not_break_the_list(
    mcp: McpSession, fake_trakt: FakeTrakt, bad_entry: Any
) -> None:
    fake_trakt.playback.insert(0, bad_entry)
    items = ok(await mcp.call("get_playback"))["items"]
    assert [i["playback_id"] for i in items] == [501, 500]


async def test_missing_paused_at_is_empty_not_none(mcp: McpSession, fake_trakt: FakeTrakt) -> None:
    fake_trakt.playback[1]["paused_at"] = None
    items = ok(await mcp.call("get_playback"))["items"]
    assert items[1]["paused_at"] == ""


async def test_malformed_entries_are_skipped_in_other_lists(
    mcp: McpSession, fake_trakt: FakeTrakt, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = fake_trakt._api

    def broken(request: Any, path: str, query: Any) -> Any:
        response = original(request, path, query)
        if request.method == "GET" and path in ("/sync/progress/up_next", "/sync/history"):
            data = [*response.json(), {"type": "movie", "movie": {"title": "x"}}, {"show": None}, 3]
            return httpx.Response(200, json=data)
        if request.method == "GET" and path.startswith(("/search/", "/sync/watchlist/")):
            return httpx.Response(
                200, json=[*response.json(), {"type": "movie", "movie": {}}, None]
            )
        return response

    monkeypatch.setattr(fake_trakt, "_api", broken)
    ok(await mcp.call("add_to_watchlist", items=[{"type": "movie", "trakt_id": 1}]))
    assert len(ok(await mcp.call("get_up_next"))["shows"]) == 1
    assert len(ok(await mcp.call("get_history"))["items"]) == 2
    assert len(ok(await mcp.call("search", query="dune"))["results"]) == 1
    assert len(ok(await mcp.call("get_watchlist"))["items"]) == 1


async def test_all_entries_malformed_is_an_error_not_an_empty_list(
    mcp: McpSession, fake_trakt: FakeTrakt, caplog: pytest.LogCaptureFixture
) -> None:
    fake_trakt.playback[:] = [{"id": n, "type": "movie", "movie": {"title": "x"}} for n in (1, 2)]
    message = error_text(await mcp.call("get_playback"))
    assert "unexpected format" in message
    [record] = [r for r in caplog.records if "malformed" in r.getMessage()]
    assert record.getMessage() == "Skipped 2 of 2 malformed Trakt list entries: KeyError x2"


async def test_unknown_shapes_alone_still_give_an_empty_list(
    mcp: McpSession, fake_trakt: FakeTrakt
) -> None:
    fake_trakt.playback[:] = [{"id": 1, "type": "season"}]  # not malformed, just not ours
    assert ok(await mcp.call("get_playback"))["items"] == []
