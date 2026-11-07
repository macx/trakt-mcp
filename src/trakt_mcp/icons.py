"""Icons for MCP clients (connector lists) and browsers.

Clients look for an icon in the MCP server metadata or fall back to the host's
favicon - and without one, some fall back to the icon of the parent domain.
We therefore always serve our own. The bundled icon is the project's own
(deliberately not the Trakt logo: Trakt's branding guidelines forbid using it
as the logo of another service). Operators can replace it by pointing
``ICON_DIR`` at a directory with any of the files in :data:`ICON_FILES`.
"""

from __future__ import annotations

import html
from collections.abc import Awaitable, Callable
from importlib import resources
from pathlib import Path

from mcp_types import Icon
from starlette.requests import Request
from starlette.responses import HTMLResponse, Response

ICON_FILES = {
    "favicon.svg": "image/svg+xml",
    "favicon.ico": "image/x-icon",
    "apple-touch-icon.png": "image/png",
}
_CACHE = "public, max-age=86400"


def load_icons(icon_dir: Path | None) -> dict[str, bytes]:
    """Read each icon from ``icon_dir`` if present there, else the bundled default."""
    bundled = resources.files("trakt_mcp") / "static"
    icons: dict[str, bytes] = {}
    for name in ICON_FILES:
        custom = icon_dir / name if icon_dir else None
        icons[name] = (
            custom.read_bytes() if custom and custom.is_file() else (bundled / name).read_bytes()
        )
    return icons


def mcp_icons(base_url: str) -> list[Icon]:
    """Icon entries for the MCP server metadata (``initialize`` result)."""
    return [
        Icon(src=f"{base_url}/favicon.svg", mime_type="image/svg+xml", sizes=["any"]),
        Icon(src=f"{base_url}/apple-touch-icon.png", mime_type="image/png", sizes=["180x180"]),
    ]


def icon_endpoint(name: str, content: bytes) -> Callable[[Request], Awaitable[Response]]:
    async def serve(_: Request) -> Response:
        return Response(content, media_type=ICON_FILES[name], headers={"Cache-Control": _CACHE})

    return serve


def home_page(title: str) -> HTMLResponse:
    """A minimal landing page, mainly so crawlers find our icon links."""
    name = html.escape(title)
    body = (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{name}</title>"
        "<link rel='icon' href='/favicon.svg' type='image/svg+xml'>"
        "<link rel='icon' href='/favicon.ico' sizes='48x48'>"
        "<link rel='apple-touch-icon' href='/apple-touch-icon.png'>"
        "</head><body style='font-family:system-ui;max-width:32rem;margin:3rem auto;"
        f"padding:0 1rem'><h1>{name}</h1><p>This is an MCP server. Add "
        "<code>/mcp</code> on this host as a connector in your AI app.</p></body></html>"
    )
    return HTMLResponse(body, headers={"Cache-Control": _CACHE})
