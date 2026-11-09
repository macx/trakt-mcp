# Security

## Reporting a vulnerability

Please do **not** open a public issue. Use GitHub's private vulnerability reporting
("Security" tab → "Report a vulnerability"). You should get a reply within a week.

## Threat model

trakt-mcp is a single-tenant service you host for yourself (and perhaps family).
It is reachable from the internet so that hosted AI clients can call it.

Protected assets: the Trakt account(s) of allowlisted users (watchlist, history and
paused playbacks can be changed) and their Trakt tokens.

### Controls

| Threat | Control |
|---|---|
| Stranger signs in with their own Trakt account | `ALLOWED_TRAKT_USERS` allowlist of Trakt **slugs** (not display names), checked at sign-in **and** on every request; the rejected user's Trakt token is revoked |
| Confused deputy: attacker registers their own client (possibly with the same redirect URI as yours, e.g. claude.ai's) and sends you a link | After the Trakt sign-in, every client a user has not approved before gets a **consent page** (client name, redirect host, Trakt account, registration time). Approval is stored per (client, Trakt user), so only an allowlisted, signed-in user can approve. The form's CSRF token is bound to the request and to a `__Host-`, `SameSite=Strict` cookie. Redirect URIs must also be on `ALLOWED_REDIRECT_URIS` |
| Device-code phishing (attacker starts a sign-in and tricks you into entering *their* code at trakt.tv/activate, or sends you the link to *their* sign-in page) | **Owner passphrase**: a sign-in only gets a Trakt device code after the passphrase was entered on our sign-in page (salted scrypt hash, ≥ 12 characters). Entering it binds the rest of the sign-in (device code page, polling, consent) to that browser via a `__Host-`, `SameSite=Strict`, HttpOnly cookie, so whoever else knows the request ID cannot continue it. The passphrase page names the requesting client and its redirect host. Wrong guesses: 5 per sign-in, 10 per IP (IPv6: per /64) and 100 server-wide per hour, checked and recorded under one lock |
| Code interception / injection | PKCE (S256) required for MCP clients; random 256-bit request IDs, single use, 10-minute lifetime; Trakt is polled server-side, never faster than its interval |
| Code or refresh token replay | Codes single use (5 min); refresh tokens rotate, replay revokes the whole token family (a retry within 10 s is refused without revoking); every sign-in ends after 90 days at the latest |
| Token theft from the database | Our tokens are stored only as SHA-256 hashes; DB file mode 0600 in a 0700 directory, owned by an unprivileged user |
| Token used against another server | Tokens are bound to this server's resource URL (RFC 8707) |
| DNS rebinding / cross-site requests | `Host` and `Origin` header allowlists on the MCP endpoint |
| Registration / authorization flooding | Unapproved clients beyond `MAX_REGISTERED_CLIENTS` are evicted oldest-first (approved ones never) and expire after an hour; pending sign-ins are capped per client (5) and in total (`MAX_PENDING_LOGINS`), evicting the oldest, so a flood cannot lock the owner out; `state` ≤ 512 chars and a well-formed S256 challenge required; expired rows purged continuously |
| Leaking secrets | No tokens or secrets in logs or error messages; access logging is off (query strings carry OAuth codes); config errors name the variable, not its value |
| Transport downgrade, framing | HSTS, `X-Frame-Options: DENY`, `frame-ancestors 'none'`, restrictive CSP on every response |
| Unnecessary token refreshes | Only HTTP 401 triggers a Trakt token refresh (Trakt refresh tokens are single use); 403 does not |
| Container breakout impact | Non-root user, no published port, read-only root FS, all capabilities dropped (see compose example) |

### Known limitations

- **Trakt tokens are stored as issued** (they must be usable). Anyone with read
  access to the data volume can act as you on Trakt until the tokens are revoked.
  Revoke them at <https://app.trakt.tv/settings/connected-apps> if the host is compromised.
- Client secrets of dynamically registered clients are stored in plain text; they
  only matter in combination with a valid PKCE verifier and an allowlisted redirect.
- There is no per-IP rate limiting in the app itself. A sustained flood of
  `/register` + `/authorize` requests can still cancel sign-ins in progress (approved
  clients, consent steps and clients with a sign-in in progress are never evicted),
  and 100 wrong passphrases (from many IPs) block sign-ins for an hour. Neither reveals
  anything. Behind a reverse proxy, set `TRUSTED_PROXIES` to the proxy's network so the
  per-IP limit sees real client addresses. Rate-limit `/register` and `/authorize`
  at your reverse proxy if this matters to you.
- Client approvals do not expire and have no UI; to revoke one, delete the row
  from the `client_approvals` table (or delete the database to reset everything).
- The passphrase is the key to new sign-ins: keep it in a password manager and only
  type it into the sign-in page of your own server.
- The consent page is the last line of defence against a phishing link. An
  attacker's client can look exactly like yours (same name, same claude.ai
  redirect); the page therefore shows how long ago the client was registered.
  Only approve when you have just clicked "Connect" in your AI app yourself.
- Your reverse proxy may log request URLs, including `/login?request=…`. Request IDs
  are single use and short-lived, but consider not logging query strings.
- One browser can run only one sign-in at a time (the sign-in cookie is shared);
  starting a second one cancels the first.
- The sign-in, consent and OAuth endpoints are public by design.

## Release integrity

What you pull from `ghcr.io/macx/trakt-mcp` is built only by
`.github/workflows/release.yml`, on a pushed `vX.Y.Z` tag, after:

- the full CI checks (lint, strict typing, tests with coverage gate, bandit,
  pip-audit) pass on the tagged commit, and
- `scripts/check_release.py` confirms the tag matches the project version and
  `docs/security-reviews.md` records an independent security review that cleared
  this version for a full commit SHA, with nothing but that record changed since.

Hardening: actions and base images pinned by digest (updated by Dependabot),
least-privilege job permissions, no persisted credentials, no caches in the
release path, a build provenance attestation per image (verify with
`--signer-workflow` and `--source-ref`, see the README), and no `latest` tag.

Limits, by design:

- The gate runs from the tagged commit. A tag on a commit that changes the
  workflow or the gate itself would bypass it; what prevents that are the GitHub
  tag rulesets (only admins create `v*` tags; none can be moved or deleted). The
  gate catches mistakes, not a malicious maintainer.
- Re-running a failed release rebuilds the image; its digest may differ from an
  earlier attempt. Deploy the digest from the published release notes.
- Any workflow with `packages: write` could push to the package; only the
  maintainer can add or change workflows.
