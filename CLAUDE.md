# trakt-mcp — Agent Guide

Lean, self-hosted MCP server for Trakt. Read `README.md` and `docs/architecture.md`
first. Security model: `SECURITY.md`.

## Principles

- **Lean by design.** A tool exists only if an assistant needs it in a normal chat
  ("add this to my watchlist", "how many episodes are left"). Do not mirror Trakt's
  API. Discuss new tools before adding them.
- **Standard MCP only** (Streamable HTTP + OAuth per the MCP auth spec), so any
  compliant client works — Claude today, others later.
- **Security first.** Any change to `auth/`, `config.redirect_uri_allowed`,
  `storage.py` or token handling needs tests for the failure cases, not just the
  happy path, and an independent security review before merging.
- English for code, comments, docs and commit messages.

## Context footprint

Every tool's description and schemas sit in the assistant's context in *every*
chat, used or not, and every tool result adds to it. The assistant should only
consume what the task at hand needs. There is deliberately no hard limit on the
number or size of tools; instead **every new or changed tool is reviewed** for
how to keep it small. `tests/test_tool_footprint.py` enforces the review: it fails
on any change to the tool list until `tests/tool_footprint.json` is regenerated
(`UPDATE_TOOL_FOOTPRINT=1 uv run pytest tests/test_tool_footprint.py`).

Checklist before regenerating the snapshot:

1. **No new tool if a parameter will do.** Extend an existing tool when the
   intent is the same (`mark_watched` covers movies, episodes, seasons, shows).
2. **Descriptions:** one or two sentences on *when* to use the tool (and when
   not); do not repeat what the schema already says.
3. **Inputs:** as few parameters as possible, sensible defaults, enums instead
   of free text.
4. **Outputs:** only fields the assistant needs to answer or to call the next
   tool; reuse existing models (`MediaSummary` for nested movies/shows); field
   descriptions only where a name is ambiguous; long text is truncated (see
   `OVERVIEW_MAX_CHARS`). Class docstrings become schema descriptions: explain
   models in comments instead. Generic schema noise is stripped automatically
   (`schema.py`, see "Context footprint" in `docs/architecture.md`).
5. **Results at runtime:** small default `limit`s, never raw Trakt payloads.
6. **Server instructions** only for rules that span several tools.
7. Note the footprint before/after (chars from the test output) in the commit
   message.

## Design decisions (do not undo without discussing)

- **Device flow, not redirect flow.** Trakt only issues *development* apps to
  individuals (2026-11), and those may only redirect to localhost/127.0.0.1/[::1]
  (`urn:ietf:wg:oauth:2.0:oob` is rejected too). Production apps are "currently
  unavailable" (support@trakt.tv). Hence sign-in uses Trakt's device flow; do not
  reintroduce an authorization-code redirect to Trakt.
- **Owner passphrase gates every sign-in** before any device code is requested,
  and unlocking binds the rest of the sign-in (device page, polling, consent) to
  that browser via the `__Host-trakt-mcp-login` cookie. This is what defeats
  device-code phishing; an "enrollment window" was tried and found insufficient.
- **Consent after identity**, approval stored per (client_id, Trakt slug). Consent
  before sign-in, or approval per client only, allowed attackers to self-approve
  (confused deputy; claude.ai's redirect URI is shared by all its users).
- **Flooding:** anonymous entries are evicted oldest-first, never refused; approved
  clients, consent steps and clients with a live sign-in are never evicted.
- **Trakt branding** (https://app.trakt.tv/branding): the Trakt logo may only refer to
  Trakt (README "Works with"), never serve as this project's logo, favicon or icon,
  and only the unmodified files from that page may be used.
- The security design went through six rounds of adversarial review; the
  reasoning per control is in `SECURITY.md`. Keep that table current.

## Workflow

Every change must pass, locally and in CI:

```sh
uv run ruff check src tests scripts && uv run ruff format --check src tests scripts
uv run mypy && uv run mypy --strict scripts/check_release.py
uv run pytest --cov                 # coverage gate: 90 %
uv run bandit -q -c pyproject.toml -r src scripts
uv export --no-dev --no-emit-project --format requirements-txt > /tmp/req.txt && uv run pip-audit -r /tmp/req.txt
```

Tests run against `tests/fake_trakt.py`, an in-memory fake of Trakt's OAuth host and
API used as an httpx transport. Extend the fake when you touch a new endpoint, and
check request/response shapes against Trakt's official contracts at
<https://github.com/trakt/api-help> (`projects/api/src/contracts/`).

## Releases

Releases are gated by CI, not by convention: `.github/workflows/release.yml` runs on
a pushed tag `vX.Y.Z` and publishes only if every gate passes. The steps:

1. Bump `pyproject.toml` and `src/trakt_mcp/__init__.py`, `uv lock`; if the minor
   version changed, update the image tag in `compose.example.yml` (and the version
   examples in the README).
2. All workflow checks green, locally and in CI.
3. An **independent adversarial security review** of the diff since the last
   cleared version: a fresh reviewer that gets the diff, the code and
   `SECURITY.md`, not our own conclusions. Fix the findings (with tests) and have
   the fixes re-checked. Workflow and script changes are in scope too.
4. Record it in `docs/security-reviews.md`:
   `| X.Y.Z | date | scope | **cleared** (reviewed commit `<full 40-char sha>`) |`, commit, and
   tag that commit `vX.Y.Z`. `scripts/check_release.py` rejects the tag if the
   versions differ, the row is missing, or anything but the review record changed
   since the reviewed commit.
5. Push the tag. The workflow reruns all checks, builds the image for amd64 and
   arm64, pushes it to `ghcr.io/macx/trakt-mcp` (`X.Y.Z`, `X.Y`) with a build
   provenance attestation, and creates the GitHub release with the digest.

Deployments use that published image by digest, never a local build. Tags `v*`
are protected on GitHub (only admins create them; none can be moved or deleted).
The gate runs from the tagged commit, so it prevents mistakes, not a malicious
maintainer - see "Release integrity" in `SECURITY.md`. A failed release is never
fixed by moving the tag: bump the patch version.

## Gotchas

- MCP Python SDK **2.x**: `FastMCP` is now `mcp.server.mcpserver.MCPServer`; types live
  in `mcp_types`; tool listings use `input_schema`/`output_schema`.
- The ASGI lifespan owns an anyio task group: in tests it must be entered and exited
  in the same task (see the `app` fixture in `tests/conftest.py`).
- Trakt: OAuth calls go to `auth.trakt.tv`, API calls to `api.trakt.tv`. HTTP 420 means
  an account limit (e.g. full watchlist) was hit. Only 401 may trigger a token
  refresh (Trakt refresh tokens are single use); 403 must not.
- Time in tests: `build_app(..., clock=...)` injects the store's clock; use the
  `clock` fixture (`clock.now += n`) instead of monkeypatching `store.now`.
- `trakt_mcp.__main__.main(argv)` parses `argv`; tests must pass `[]` explicitly.

## Deployment

The repository is deployment-agnostic: `Dockerfile` and `compose.example.yml` are the
reference. Keep operator-specific details (hostnames, servers, credentials, where an
instance runs) out of this repository; they belong in the operator's own notes.
