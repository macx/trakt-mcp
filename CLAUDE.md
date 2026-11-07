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
uv run ruff check src tests && uv run ruff format --check src tests
uv run mypy                         # strict
uv run pytest --cov                 # coverage gate: 90 %
uv run bandit -q -c pyproject.toml -r src
uv export --no-dev --no-emit-project --format requirements-txt > /tmp/req.txt && uv run pip-audit -r /tmp/req.txt
```

Tests run against `tests/fake_trakt.py`, an in-memory fake of Trakt's OAuth host and
API used as an httpx transport. Extend the fake when you touch a new endpoint, and
check request/response shapes against Trakt's official contracts at
<https://github.com/trakt/api-help> (`projects/api/src/contracts/`).

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
