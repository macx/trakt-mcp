# Architecture

## Modules

```
src/trakt_mcp/
├── __main__.py        CLI: serve (default), set-passphrase
├── config.py          Settings (env vars only) + redirect URI allowlist matching
├── server.py          wiring: store → Trakt clients → auth provider → LeanMCPServer → ASGI app
├── schema.py          compacts the tool schemas advertised to the assistant
├── storage.py         SQLite: clients, pending logins, hashed grants, Trakt tokens
├── auth/provider.py   OAuth authorization server; device sign-in; consent
├── auth/passphrase.py owner passphrase (scrypt) that gates every sign-in
├── auth/pages.py      passphrase, sign-in code, consent and error HTML pages
├── trakt/http.py      TraktOAuth (device flow, refresh, revoke) and TraktApi, error mapping
├── trakt/sessions.py  per-user Trakt token refresh (serialised; refresh tokens are single use)
├── trakt/service.py   the Trakt operations behind the tools; response → compact models
├── models.py          tool input/output models (become the tools' JSON schemas)
└── tools.py           MCP tool definitions and Trakt error → assistant message mapping
```

Dependencies point inwards: `tools` → `service` → `sessions`/`http` → `storage`.
Nothing below `server.py` knows about HTTP routing.

## Sign-in sequence

1. **Discovery.** The client calls `/mcp`, gets `401` with
   `WWW-Authenticate: Bearer resource_metadata=…`, reads
   `/.well-known/oauth-protected-resource/mcp` and `/.well-known/oauth-authorization-server`.
2. **Registration** (`POST /register`, RFC 7591). Rejected unless every redirect URI
   matches `ALLOWED_REDIRECT_URIS`. Unapproved clients are evicted oldest-first beyond
   the cap and expire after an hour.
3. **Authorize** (`GET /authorize`). The SDK validates client, redirect URI, PKCE
   challenge and scope. The request is stored under a random request ID → redirect to
   `/login?request=…`.
4. **Passphrase** (`GET`/`POST /login`). The page first asks for the owner passphrase
   (scrypt-verified, one at a time; 5 wrong tries per sign-in, 20 per hour). Nothing is
   requested from Trakt before that, which defeats device-code phishing.
4a. **Device sign-in** (`GET /login`). Once unlocked, the next visit requests a Trakt
   device code and shows the user code plus a link to trakt.tv/activate. The page
   reloads itself (`<meta refresh>`, no JavaScript); each reload polls Trakt's token
   endpoint, at most once per Trakt interval (backing off on `slow_down`). Denied or
   expired codes send the user back to the client with `access_denied`.
5. **Identity.** With Trakt's tokens we read `/users/settings`, enforce
   `ALLOWED_TRAKT_USERS` (by slug) and store the Trakt tokens.
6. **Consent** (only if this Trakt user has not approved this client before). The page names the client, its redirect host, the Trakt account
   and how long ago the client registered. Its form (`POST /consent`) carries a CSRF
   token that must match both the pending consent and a `__Host-`/`SameSite=Strict`
   cookie. Approval is stored per (client, Trakt user).
7. Mint our authorization code (5 min) and redirect to the client.
8. **Token** (`POST /token`). The SDK checks client authentication and the PKCE
   verifier; we consume the code and issue an access token (1 h) and a refresh token
   (30 days). Both belong to a *family* (one per sign-in).
9. **Refresh.** The old refresh token is marked used and a new pair is issued. Presenting
   a used refresh token again revokes the whole family, unless it is a retry within
   10 seconds. No token outlives the sign-in's absolute lifetime (90 days).

## Trakt tokens

Trakt access tokens are valid for 24 h; refresh tokens are single use. `TraktSessions`
refreshes 5 minutes before expiry under a per-user lock, retries a request once after a
`401`, and — if Trakt rejects the refresh token — deletes the user's Trakt tokens and
revokes all MCP tokens of that user, so the client is asked to sign in again.

## Transport

Streamable HTTP in stateless JSON mode (`/mcp`), so the server keeps no per-session
state in memory and survives restarts transparently.

## Context footprint

Everything the server advertises or returns ends up in the assistant's context, so
`LeanMCPServer` (in `server.py`) trims what carries no meaning for the model:

- **Tool list:** `schema.compact_schema` drops pydantic's auto-generated `title`s,
  turns `anyOf [X, null]` for scalars into `type: [X, "null"]`, and drops `default`s
  from output schemas. Validation still uses the full models.
- **Results:** the text copy of a structured result (for clients that only read
  text) is compact JSON instead of the SDK's indented JSON - same data, roughly a
  third smaller.
- **Models:** nested movies/shows use `MediaSummary` (type, title, year, Trakt ID);
  only `search` returns slug, IMDb ID and overview, which help to pick the right
  title. Class docstrings would become schema descriptions, so models are explained
  in comments.

`tests/test_tool_footprint.py` makes every change to the tool list visible; see
"Context footprint" in `CLAUDE.md`.

### Evaluated, not adopted (2026-11)

- **TOON** (Token-Oriented Object Notation) for the text copy of results.
  Measured on realistic results (50 watchlist entries, 10 history entries, …) with
  `@toon-format/toon` 4.4.0 and the `o200k_base` tokenizer as a proxy (no Claude
  tokenizer available offline), against the compact JSON we send:

  | Result | compact JSON | TOON | TOON, flat episode fields |
  |---|---|---|---|
  | `get_watchlist` | 2119 | 1526 (−28 %) | – |
  | `get_up_next` | 810 | 464 (−43 %) | – |
  | `search` | 311 | 238 (−23 %) | – |
  | `get_history` | 528 | 630 (+19 %) | 366 (−31 %) |
  | `get_playback` | 379 | 455 (+20 %) | 258 (−32 %) |

  TOON only pays off for uniform rows; the optional nested `episode` makes it
  *larger* unless episodes are flattened (`season`, `episode`, `episode_title`),
  which in turn makes plain JSON slightly larger. Not adopted because: the absolute
  savings are small (typically 100-600 tokens per call, versus ~4.5k for the tool
  list in every chat); it is unknown whether a given client feeds the model the text
  copy or `structuredContent` (which must stay JSON); a 2026 study reports accuracy
  drops of up to 9 points against JSON; and Apify removed TOON from its MCP server
  after a 30-day production trial without a reliable benefit
  (<https://aaif.io/blog/we-added-toon-to-our-mcp-server-then-removed-it>). Worth
  revisiting if results grow (larger lists) or clients document which copy they use;
  measure with the client's real tokenizer first.
- **Jev** (TypeSafe's decision-only model returning typed answers / yes-no
  probabilities). It is a separate model call and does not shrink what the assistant
  reads; our results are deterministic Trakt data, not generated text. The only
  candidate - picking the right `search` hit server-side - would save ~250 tokens per
  search, but remakes are told apart by year and date comparison is a documented Jev
  weakness; a wrong pick would change the watchlist without the user being asked. It
  would also add an external API key and send queries to a third party.

## Reference

Trakt's official, typed API contracts: <https://github.com/trakt/api-help>.
