"""Compacted tool schemas: smaller, but with the same meaning."""

import json
from typing import Any

import jsonschema
import pytest

from tests.conftest import McpSession
from trakt_mcp.schema import compact_schema


def test_auto_titles_are_dropped_but_a_property_called_title_is_kept() -> None:
    schema = {
        "title": "EpisodeRef",
        "type": "object",
        "properties": {"title": {"title": "Title", "type": "string"}},
        "$defs": {"title": {"title": "Nested", "type": "integer"}},
    }
    assert compact_schema(schema) == {
        "type": "object",
        "properties": {"title": {"type": "string"}},
        "$defs": {"title": {"type": "integer"}},
    }


def test_nullable_scalar_becomes_a_type_list() -> None:
    schema = {"anyOf": [{"type": "string"}, {"type": "null"}], "default": None, "description": "d"}
    assert compact_schema(schema) == {
        "type": ["string", "null"],
        "default": None,
        "description": "d",
    }


@pytest.mark.parametrize(
    "schema",
    [
        {"anyOf": [{"$ref": "#/$defs/EpisodeRef"}, {"type": "null"}]},  # not a scalar
        {"anyOf": [{"type": "integer", "minimum": 1}, {"type": "null"}]},  # carries bounds
        {"anyOf": [{"type": "null"}, {"type": "null"}]},
        {"anyOf": [{"type": "string"}, {"type": "integer"}]},
        {"anyOf": [{"type": "string"}, {"type": "null"}, {"type": "integer"}]},
        {"anyOf": [{"type": "string"}, {"type": "null"}], "type": "string"},
        {"anyOf": [{"type": ["string", "integer"]}, {"type": "null"}]},
        {"anyOf": "not-a-list"},
    ],
)
def test_other_unions_are_left_alone(schema: dict[str, Any]) -> None:
    assert compact_schema(schema) == schema


def test_defaults_are_dropped_only_from_output_schemas() -> None:
    schema = {"type": "object", "properties": {"limit": {"type": "integer", "default": 10}}}
    assert compact_schema(schema)["properties"]["limit"]["default"] == 10
    assert "default" not in compact_schema(schema, output=True)["properties"]["limit"]


def test_data_values_are_never_rewritten() -> None:
    schema = {
        "type": "object",
        "default": {"title": "kept", "x": None},
        "examples": [{"title": "kept"}],
        "const": {"title": "kept"},
        "enum": [{"title": "kept"}],
    }
    assert compact_schema(schema) == schema


def test_input_is_not_mutated() -> None:
    schema = {"title": "X", "properties": {"a": {"title": "A", "type": "string"}}}
    compact_schema(schema, output=True)
    assert schema == {"title": "X", "properties": {"a": {"title": "A", "type": "string"}}}


def _keywords(node: Any, key: str) -> int:
    """Count ``key`` used as a schema keyword (not as a property name)."""
    if isinstance(node, list):
        return sum(_keywords(item, key) for item in node)
    if not isinstance(node, dict):
        return 0
    count = int(key in node and isinstance(node[key], str))
    for name, value in node.items():
        if name in ("properties", "$defs") and isinstance(value, dict):
            count += sum(_keywords(sub, key) for sub in value.values())
        else:
            count += _keywords(value, key)
    return count


async def test_advertised_tools_carry_no_auto_titles(mcp: McpSession) -> None:
    tools = (await mcp.rpc("tools/list")).json()["result"]["tools"]
    for tool in tools:
        assert tool["title"]  # the tool's own human-readable title stays
        assert _keywords(tool["inputSchema"], "title") == 0, tool["name"]
        assert _keywords(tool["outputSchema"], "title") == 0, tool["name"]
        jsonschema.Draft202012Validator.check_schema(tool["inputSchema"])
        jsonschema.Draft202012Validator.check_schema(tool["outputSchema"])
    progress = next(t for t in tools if t["name"] == "get_show_progress")
    assert "title" in progress["outputSchema"]["$defs"]["EpisodeRef"]["properties"]


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("search", {"query": "a"}),
        ("get_watchlist", {}),
        ("get_show_progress", {"show_trakt_id": 10}),
        ("get_up_next", {}),
        ("get_history", {}),
        ("get_playback", {}),
        ("get_stats", {}),
        ("remove_playback", {"playback_ids": [500, 9999]}),
        ("mark_watched", {"movie_trakt_ids": [1]}),
        ("unmark_watched", {"movie_trakt_ids": [1]}),
        ("add_to_watchlist", {"items": [{"type": "movie", "trakt_id": 1}]}),
        ("remove_from_watchlist", {"items": [{"type": "movie", "trakt_id": 1}]}),
    ],
)
async def test_real_results_match_the_compacted_output_schema(
    mcp: McpSession, tool: str, arguments: dict[str, Any]
) -> None:
    tools = (await mcp.rpc("tools/list")).json()["result"]["tools"]
    schema = next(t for t in tools if t["name"] == tool)["outputSchema"]
    result = await mcp.call(tool, **arguments)
    assert result["isError"] is False, result
    jsonschema.validate(result["structuredContent"], schema)


async def test_text_copy_of_a_result_is_compact_and_identical(mcp: McpSession) -> None:
    result = await mcp.call("get_history", limit=10)
    [block] = result["content"]
    assert block["type"] == "text"
    assert json.loads(block["text"]) == result["structuredContent"]
    assert "\n" not in block["text"] and ": " not in block["text"].replace("\\", "")


async def test_errors_keep_their_message(mcp: McpSession) -> None:
    result = await mcp.call("mark_watched")
    assert result["isError"] is True
    text = result["content"][0]["text"]
    assert text.endswith("Pass at least one movie, episode, season or show.")
