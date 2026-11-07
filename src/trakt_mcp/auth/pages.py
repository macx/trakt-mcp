"""The HTML pages this server renders: passphrase, sign-in code, consent and error.

Every interpolated value is HTML-escaped. The pages need no JavaScript; the
Content-Security-Policy only allows inline styles and posting the form to
ourselves (plus the MCP client's origin, where the decision redirects to).
"""

from __future__ import annotations

import html
from datetime import UTC, datetime

from starlette.responses import HTMLResponse

_STYLE = (
    # Trakt's current brand colour (2025 redesign), with an sRGB fallback.
    ":root{--accent:#6438d0;--accent:oklch(49% .217 289)}"
    "body{font-family:system-ui,sans-serif;max-width:34rem;margin:3rem auto;padding:0 1rem;"
    "line-height:1.5;color:#1d1d1f;background:#fff}"
    "@media (prefers-color-scheme:dark){body{color:#f2f2f2;background:#161616}}"
    "dl{display:grid;grid-template-columns:auto 1fr;gap:.25rem 1rem}dt{font-weight:600}"
    "dd{margin:0;overflow-wrap:anywhere}.warn{padding:.75rem 1rem;border-left:4px solid #d97706}"
    "button{font:inherit;padding:.6rem 1.2rem;margin-right:.5rem;border-radius:.4rem;"
    "border:1px solid #888;cursor:pointer}button.primary{background:var(--accent);color:#fff;"
    "border-color:var(--accent)}"
    "a.button{display:inline-block;text-decoration:none;padding:.6rem 1.2rem;"
    "border-radius:.4rem;background:var(--accent);color:#fff}"
    ".code{font:600 2rem ui-monospace,monospace;"
    "letter-spacing:.2em}.muted{opacity:.7}input[type=password]{font:inherit;padding:.5rem;"
    "width:100%;max-width:20rem}"
)


def _page(
    title: str, body: str, *, status: int, form_targets: str = "", refresh: int | None = None
) -> HTMLResponse:
    meta_refresh = f"<meta http-equiv='refresh' content='{int(refresh)}'>" if refresh else ""
    document = (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"{meta_refresh}"
        f"<title>{html.escape(title)} · trakt-mcp</title><style>{_STYLE}</style></head>"
        f"<body>{body}</body></html>"
    )
    csp = "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'; " + (
        f"form-action 'self' {form_targets}".strip()
    )
    return HTMLResponse(
        document,
        status_code=status,
        headers={"Cache-Control": "no-store", "Content-Security-Policy": csp},
    )


def error_page(message: str) -> HTMLResponse:
    body = f"<h1>Sign-in failed</h1><p>{html.escape(message)}</p>"
    return _page("Sign-in failed", body, status=400)


def device_page(*, user_code: str, verification_url: str, refresh_seconds: int) -> HTMLResponse:
    esc = html.escape
    url = (
        verification_url if verification_url.startswith("https://") else "https://trakt.tv/activate"
    )
    body = (
        "<h1>Sign in with Trakt</h1>"
        "<p>1. Open Trakt and enter this code:</p>"
        f"<p class='code'>{esc(user_code)}</p>"
        f"<p><a class='button primary' href='{esc(url)}' target='_blank' rel='noopener noreferrer'>"
        "Open Trakt</a></p>"
        "<p>2. Approve the app on Trakt, then come back to this page. It continues "
        "automatically.</p><p class='muted'>Waiting for Trakt…</p>"
    )
    return _page("Sign in", body, status=200, refresh=refresh_seconds)


def passphrase_page(
    *, request_id: str, error: str | None, client_name: str, redirect_host: str
) -> HTMLResponse:
    esc = html.escape
    message = f"<p class='warn'>{esc(error)}</p>" if error else ""
    body = (
        "<h1>Sign in</h1>"
        "<p>Enter the passphrase of this server to connect this app to Trakt:</p>"
        f"<dl><dt>Application</dt><dd>{esc(client_name)}</dd>"
        f"<dt>Returns to</dt><dd>{esc(redirect_host)}</dd></dl>"
        "<p class='warn'>Only continue if you opened this page yourself, from your AI app "
        "- not from a link someone sent you.</p>"
        f"{message}<form method='post' action='/login'>"
        f"<input type='hidden' name='request' value='{esc(request_id)}'>"
        "<p><input type='password' name='passphrase' autocomplete='current-password' "
        "required autofocus aria-label='Passphrase'></p>"
        "<button class='primary' type='submit'>Continue</button></form>"
    )
    return _page("Sign in", body, status=400 if error else 200)


def consent_page(
    *,
    request_id: str,
    csrf_token: str,
    client_name: str,
    redirect_host: str,
    registered_at: int,
    account: str,
    form_target: str,
    now: int,
) -> HTMLResponse:
    registered = datetime.fromtimestamp(registered_at, tz=UTC).strftime("%Y-%m-%d %H:%M UTC")
    age = _age(now - registered_at)
    esc = html.escape
    body = (
        "<h1>Allow access to your Trakt account?</h1>"
        f"<p>An application wants to manage your Trakt watchlist and history through "
        f"this server.</p><dl><dt>Application</dt><dd>{esc(client_name)}</dd>"
        f"<dt>Returns to</dt><dd>{esc(redirect_host)}</dd>"
        f"<dt>Trakt account</dt><dd>{esc(account)}</dd>"
        f"<dt>Registered</dt><dd><strong>{esc(age)}</strong> ({esc(registered)})</dd></dl>"
        "<p class='warn'><strong>Only continue if you started connecting this yourself, "
        "just now, in your AI app.</strong> If you opened this page from a link someone sent "
        "you, choose Deny.</p>"
        "<form method='post' action='/consent'>"
        f"<input type='hidden' name='request' value='{esc(request_id)}'>"
        f"<input type='hidden' name='csrf' value='{esc(csrf_token)}'>"
        "<button class='primary' type='submit' name='action' value='approve'>"
        "Allow</button>"
        "<button type='submit' name='action' value='deny'>Deny</button></form>"
    )
    return _page("Allow access?", body, status=200, form_targets=form_target)


_MINUTE, _HOUR, _DAY = 60, 3600, 86400


def _age(seconds: int) -> str:
    """Human-readable age; a client you just connected was registered moments ago."""
    if seconds < 2 * _MINUTE:
        return "just now"
    if seconds < 2 * _HOUR:
        return f"{seconds // _MINUTE} minutes ago"
    if seconds < 2 * _DAY:
        return f"{seconds // _HOUR} hours ago"
    return f"{seconds // _DAY} days ago"
