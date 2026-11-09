# Security reviews

Every release gets an independent adversarial security review of the diff since the
last cleared release (see "Releases" in `CLAUDE.md`). The release workflow enforces
it: `scripts/check_release.py` only lets a tag `vX.Y.Z` through if a row below reads
`| X.Y.Z | … | **cleared** (reviewed commit `<sha>`) |` and nothing but this file
changed between that commit and the tag.

| Version | Date | Scope (diff) | Result |
|---|---|---|---|
| 0.4.1 | 2026-11-08 | `f0c74a3..v0.4.1` (0.2.1 → 0.4.1: playback tools, `LeanMCPServer`, schema compaction, `MediaSummary`) | **cleared** (reviewed commit `4fcf3b1`) |

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

Re-review of the fixes (`d2661ee..5092e11`): all four fixed. New finding:

5. **Low** - the broad `except` in `_parse_each` turned a Trakt format change or a
   parser regression into a silently *empty* result, which an assistant would act
   on (e.g. a duplicate `mark_watched`). Fixed in `4fcf3b1`: if entries came back
   but none could be parsed, a `TraktError` is raised; skipped entries are logged
   once per call (counts and exception types only, no content).

Re-review of `5092e11..4fcf3b1`: fixed, no new issues. **Verdict: cleared.**

Accepted residuals (reviewer did not object):

- IDs use pydantic's lax integers (`true` → 1, `"5"` → 5), like every ID parameter;
  a client gains nothing it could not get by sending the integer.
- In `remove_playback`, errors other than rate limit/outage (auth, 403, other 4xx)
  mid-loop still raise and hide deletions already made; rare, and a retry is safe
  (already removed IDs come back as `not_found`).
- A response that is not a list, or only non-objects, counts as "unknown shape" and
  yields an empty result rather than an error.

Checked without findings: path/parameter injection into Trakt URLs, cross-user
access, request amplification, the `list_tools`/`call_tool` overrides (validation,
error handling, `Mcp-Param` header validation, auth untouched), logging, disclosure.

0.2.1 and earlier were reviewed in the six rounds documented in `SECURITY.md`.
0.3.0 was deployed before this review existed; it is covered by the review above.
