"""Compact JSON schemas for the tool list the assistant reads.

Pydantic schemas carry noise that costs context in every chat but tells the
model nothing: an auto-generated ``title`` on every field ("Trakt Id" next to
``trakt_id``), ``anyOf`` with ``null`` for optional scalars, and ``default``
values in *output* schemas. :func:`compact_schema` removes exactly that and
nothing with meaning (types, enums, bounds, descriptions, ``required``).

Only the advertised copy is compacted; the server keeps validating arguments
against the full models.
"""

from __future__ import annotations

from typing import Any, cast

# Keywords whose value maps *names* to schemas; a key "title" in there is a
# property called title, not the title keyword.
_SCHEMA_MAPS = frozenset({"properties", "$defs", "definitions", "patternProperties"})
# Keywords whose value is data, not a schema: never rewritten.
_DATA_KEYWORDS = frozenset({"default", "examples", "const", "enum"})
_NULL = {"type": "null"}


def compact_schema(schema: dict[str, Any], *, output: bool = False) -> dict[str, Any]:
    """Return a compacted copy of ``schema``; ``output`` also drops ``default``s."""
    return cast("dict[str, Any]", _compact(schema, output=output))


def _compact(node: Any, *, output: bool) -> Any:
    if isinstance(node, list):
        return [_compact(item, output=output) for item in node]
    if not isinstance(node, dict):
        return node
    compacted: dict[str, Any] = {}
    for key, value in node.items():
        if (key == "title" and isinstance(value, str)) or (output and key == "default"):
            continue
        if key in _DATA_KEYWORDS:
            compacted[key] = value
        elif key in _SCHEMA_MAPS and isinstance(value, dict):
            compacted[key] = {name: _compact(sub, output=output) for name, sub in value.items()}
        else:
            compacted[key] = _compact(value, output=output)
    return _nullable_scalar(compacted)


def _nullable_scalar(node: dict[str, Any]) -> dict[str, Any]:
    """``{"anyOf": [{"type": "string"}, {"type": "null"}]}`` -> ``{"type": ["string", "null"]}``."""
    variants = node.get("anyOf")
    if not (isinstance(variants, list) and len(variants) == 2 and _NULL in variants):  # noqa: PLR2004
        return node
    others = [v for v in variants if v != _NULL]
    if "type" in node or len(others) != 1:
        return node
    [other] = others
    if not (isinstance(other, dict) and set(other) == {"type"} and isinstance(other["type"], str)):
        return node
    rest = {k: v for k, v in node.items() if k != "anyOf"}
    return {"type": [other["type"], "null"], **rest}
