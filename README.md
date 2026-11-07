# trakt-mcp

<p>
  <sub>Works with</sub><br>
  <a href="https://trakt.tv">
    <picture>
      <source media="(prefers-color-scheme: dark)" srcset="docs/assets/trakt-logo-dark.svg">
      <img alt="Trakt" src="docs/assets/trakt-logo.svg" height="32">
    </picture>
  </a>
</p>

A lean, self-hosted [MCP](https://modelcontextprotocol.io) server that lets your AI
assistant manage your [Trakt](https://trakt.tv) watchlist and answer questions about
your watch progress — from Claude on your phone, Claude Code, or any other MCP client.

> "I just heard about *Severance* — put it on my watchlist."
> "How many episodes of *Severance* do I have left?"

It is deliberately **not** a wrapper around all of Trakt's 300+ endpoints. Ten
focused tools keep the assistant's context small and its tool choices reliable.

> **Status:** early (0.x), built for personal self-hosting. Provided as-is.

## Tools

| Tool | What it does |
|---|---|
| `search` | Find movies/shows by title (optionally type and year). Returns Trakt IDs. |
| `get_watchlist` | Your watchlist, newest first, with its size and your account's limit. |
| `add_to_watchlist` | Add movies/shows by Trakt ID. Reports a full watchlist clearly. |
| `remove_from_watchlist` | Remove movies/shows by Trakt ID. |
| `get_show_progress` | Watched / aired / remaining episodes, next episode, per-season breakdown. |
| `get_history` | What you watched most recently (movies and episodes). |
| `get_up_next` | Shows you are in the middle of, with what is next. |
| `mark_watched` | Record watched movies, episodes, whole seasons or whole shows (now, a date, the air date, or unknown). |
| `unmark_watched` | Remove all plays of movies, episodes, seasons or shows from the history (undo). |
| `get_stats` | Lifetime totals: movies, episodes, hours. |

All tools carry MCP annotations (`readOnlyHint`, `destructiveHint`, …) so clients can
ask for confirmation before changes.

## How sign-in works

The server is its own OAuth 2.1 authorization server (as the MCP spec requires for
remote servers) and delegates the actual login to Trakt using Trakt's **device flow**:
your AI app opens a page that shows a short code, you enter it at
<https://trakt.tv/activate>, and the page continues by itself. The device flow needs no
public redirect URL, so a Trakt **development app** is enough — which is what Trakt
hands out to individuals.

- **Owner passphrase.** Every sign-in first asks for a passphrase that only you know
  (set once with `trakt-mcp set-passphrase`). Only after that does the server request a
  Trakt code. This binds the device flow to the person who started it: nobody can trick
  you into entering *their* code at trakt.tv/activate and get anything out of it.
- **Allowlist.** Only Trakt users listed in `ALLOWED_TRAKT_USERS` can obtain a token.
- **Consent.** The first time a client connects, you see a consent page naming it
  (after signing in with Trakt). Approval is bound to that client *and* your Trakt
  account.
- PKCE, single-use codes, rotating refresh tokens with replay detection, tokens stored
  only as SHA-256 hashes, 90-day maximum session. See [SECURITY.md](SECURITY.md) and
  [docs/architecture.md](docs/architecture.md).

## Setup

### 1. Create a Trakt app

At <https://app.trakt.tv/settings/apps> create a **Development** app:

- **Redirect URI:** `http://localhost:8000/trakt/callback` (required by the form; the
  device flow does not use it, but Trakt expects a registered value on token refresh)
- **JavaScript (CORS) origins:** leave empty
- **Permissions** (`/checkin`, `/scrobble`): not needed

Note the **Client ID** (and the client secret, which is optional). As the app owner you
can always sign in; add other people to the app's sign-in list on Trakt if needed.

### 2. Run it

```sh
cp .env.example .env   # fill in TRAKT_CLIENT_ID, PUBLIC_URL, ALLOWED_TRAKT_USERS
docker compose -f compose.example.yml up -d --build
```

Put it behind a TLS-terminating reverse proxy (see `compose.example.yml` for Caddy).
The container needs no published port, runs as an unprivileged user, works with a
read-only root filesystem and keeps its state in the `/data` volume (~50 MB RAM).

### 3. Connect your assistant

1. Set the owner passphrase once (at least 12 characters; keep it in your password
   manager): `docker exec -it trakt-mcp trakt-mcp set-passphrase`
2. **Claude (web, desktop, Android, iOS):** *Settings → Connectors → Add custom
   connector*, URL `https://<your-host>/mcp`, leave client ID/secret empty. Enter your
   passphrase, enter the code shown at trakt.tv/activate, then approve the consent page.
   The connector is then available in the mobile apps too. You sign in again after
   90 days.
3. **Claude Code:** `claude mcp add --transport http trakt https://<your-host>/mcp`
4. **Other MCP clients:** add the client's OAuth callback URL to `ALLOWED_REDIRECT_URIS`.

## Configuration

| Variable | Required | Default | Description |
|---|---|---|---|
| `TRAKT_CLIENT_ID` | yes | – | Client ID of your Trakt app |
| `TRAKT_CLIENT_SECRET` | no | – | Sent to Trakt's token endpoints if set |
| `TRAKT_REDIRECT_URI` | no | `http://localhost:8000/trakt/callback` | A redirect URI registered for your Trakt app (used on token refresh) |
| `PUBLIC_URL` | yes | – | External `https://` base URL, no path |
| `ALLOWED_TRAKT_USERS` | yes | – | Comma-separated Trakt slugs (`trakt.tv/users/<slug>`) |
| `ALLOWED_REDIRECT_URIS` | no | Claude + loopback | Comma-separated; trailing `/*` = any path below |
| `MAX_REGISTERED_CLIENTS` | no | `200` | Unapproved clients kept before the oldest are evicted |
| `ACCESS_TOKEN_TTL_SECONDS` | no | `3600` | Lifetime of MCP access tokens |
| `REFRESH_TOKEN_TTL_SECONDS` | no | `2592000` | Lifetime of MCP refresh tokens (30 days) |
| `MAX_SESSION_SECONDS` | no | `7776000` | Absolute lifetime of a sign-in (90 days) |
| `TRUSTED_PROXIES` | no | – | Comma-separated networks of your reverse proxy (e.g. `172.18.0.0/16`); their `X-Forwarded-For` is used for per-IP rate limiting |
| `MAX_PENDING_LOGINS` | no | `1000` | Sign-ins in progress kept before the oldest are evicted |
| `DATA_DIR` | no | `/data` | Where the SQLite database lives |
| `HOST` / `PORT` | no | `127.0.0.1` / `8000` | Bind address (the image uses `0.0.0.0`) |
| `ICON_DIR` | no | – | Directory with `favicon.svg`, `favicon.ico` and/or `apple-touch-icon.png` replacing the bundled icon (do not use the Trakt logo here, see License) |
| `LOG_LEVEL` | no | `INFO` | Python log level |

## Development

```sh
uv sync
uv run pytest --cov          # tests (against an in-memory fake Trakt)
uv run ruff check src tests && uv run ruff format --check src tests
uv run mypy                  # strict
uv run bandit -c pyproject.toml -r src
```

Run locally: create a `.env` with `PUBLIC_URL=http://localhost:8000`, `HOST=127.0.0.1`
and `DATA_DIR=./data`, then `uv run trakt-mcp`.

## License

[MIT](LICENSE) for the code.

The Trakt name and logo are trademarks of Trakt, Inc. The logo files in
`docs/assets/` are the unmodified downloads from the
[Trakt branding guidelines](https://app.trakt.tv/branding) and are used only to refer
to the service this project works with. Per those guidelines the Trakt logo is not
used as this project's own logo or icon. This project is not affiliated with,
sponsored or endorsed by Trakt.
