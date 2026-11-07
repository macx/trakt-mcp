# Architecture

## Modules

```
src/trakt_mcp/
├── __main__.py        CLI: serve (default), set-passphrase
├── config.py          Settings (env vars only) + redirect URI allowlist matching
├── server.py          wiring: store → Trakt clients → auth provider → MCPServer → ASGI app
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

## Reference

Trakt's official, typed API contracts: <https://github.com/trakt/api-help>.
