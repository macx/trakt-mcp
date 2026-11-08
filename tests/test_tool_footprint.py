"""Context footprint of the tool list: every change has to be reviewed.

Each tool's description and schemas end up in the assistant's context, in
every chat, whether the tool is used or not. There is deliberately no upper
limit; instead this test records the size of every tool in
``tests/tool_footprint.json`` and fails on *any* change, so a new or changed
tool cannot slip in without someone going through the "Context footprint"
checklist in ``CLAUDE.md``.

After that review, regenerate the snapshot and commit it with the change::

    UPDATE_TOOL_FOOTPRINT=1 uv run pytest tests/test_tool_footprint.py
"""

import json
import os
from pathlib import Path
from typing import Any

from tests.conftest import McpSession
from trakt_mcp.tools import SERVER_INSTRUCTIONS

SNAPSHOT = Path(__file__).with_name("tool_footprint.json")


def _size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":"))) if value else 0


def footprint(tools: list[dict[str, Any]]) -> dict[str, Any]:
    """Characters per tool, split into the parts that land in the model's context."""
    per_tool = {
        tool["name"]: {
            "description": len(tool.get("description") or ""),
            "input_schema": _size(tool.get("inputSchema")),
            "output_schema": _size(tool.get("outputSchema")),
            "total": _size(tool),
        }
        for tool in sorted(tools, key=lambda t: t["name"])
    }
    total = sum(t["total"] for t in per_tool.values()) + len(SERVER_INSTRUCTIONS)
    return {
        "_summary": {
            "tools": len(per_tool),
            "server_instructions": len(SERVER_INSTRUCTIONS),
            "total_chars": total,
            "approx_tokens": total // 4,
        },
        "tools": per_tool,
    }


async def test_tool_footprint_matches_reviewed_snapshot(mcp: McpSession) -> None:
    current = footprint((await mcp.rpc("tools/list")).json()["result"]["tools"])
    if os.environ.get("UPDATE_TOOL_FOOTPRINT"):
        SNAPSHOT.write_text(json.dumps(current, indent=2) + "\n")
    recorded = json.loads(SNAPSHOT.read_text()) if SNAPSHOT.exists() else {}
    if current != recorded:
        old, new = recorded.get("tools", {}), current["tools"]
        changes = [
            f"  {name}: {old.get(name, {}).get('total', 0)} -> {new.get(name, {}).get('total', 0)}"
            for name in sorted(set(old) | set(new))
            if old.get(name) != new.get(name)
        ]
        before = recorded.get("_summary", {}).get("total_chars", 0)
        raise AssertionError(
            "The tool list's context footprint changed (chars):\n"
            + "\n".join(changes)
            + f"\n  total: {before} -> {current['_summary']['total_chars']}\n"
            "Review it against the 'Context footprint' checklist in CLAUDE.md, then run "
            "UPDATE_TOOL_FOOTPRINT=1 uv run pytest tests/test_tool_footprint.py"
        )
