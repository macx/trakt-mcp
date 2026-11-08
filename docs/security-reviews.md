# Security reviews

Every release gets an independent adversarial security review of the diff since the
last cleared release **before it is deployed** (see "Releases" in `CLAUDE.md`). Only
versions marked *cleared* here may be deployed.

| Version | Date | Scope (diff) | Result |
|---|---|---|---|
| 0.4.1 | 2026-11-08 | `f0c74a3..v0.4.1` (0.2.1 → 0.4.1: playback tools, `LeanMCPServer`, schema compaction, `MediaSummary`) | pending re-review of the fixes |

## 0.2.1 → 0.4.1 (2026-11-08)

Reviewer: independent agent, given only the diff, the code and `SECURITY.md`.
No critical, high or medium findings. Findings on 0.4.0 and their fixes in 0.4.1:

1. **Low** - `remove_playback` hid partial progress: a rate limit or outage after
   some deletions surfaced only as an error. Fixed: the partial outcome is returned
   with `retry_later` and `retry_after_seconds`; a failure before any change is
   still raised.
2. **Low** - one malformed Trakt entry (e.g. a movie without `ids`, a non-numeric
   `progress`) broke a whole list result. Fixed: every list (search, watchlist,
   history, up next, playback) skips malformed entries (`_parse_each`).
3. **Info** - `compact_schema` recursed into data keywords (`default`, `examples`,
   `const`, `enum`). Fixed: those values are never rewritten.
4. **Info** - `remove_playback` did not ask for confirmation. Fixed: its description
   now asks to confirm with the user first.

Checked without findings: path/parameter injection into Trakt URLs, cross-user
access, request amplification, the `list_tools`/`call_tool` overrides (validation,
error handling, `Mcp-Param` header validation, auth untouched), logging, disclosure.

0.2.1 and earlier were reviewed in the six rounds documented in `SECURITY.md`.
0.3.0 was deployed before this review existed; it is covered by the review above.
