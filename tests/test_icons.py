from pathlib import Path

import httpx
import pytest

from tests.conftest import BASE_URL, McpSession, Tokens
from trakt_mcp.icons import ICON_FILES, load_icons


@pytest.mark.parametrize(("name", "media_type"), list(ICON_FILES.items()))
async def test_icons_are_served(client: httpx.AsyncClient, name: str, media_type: str) -> None:
    response = await client.get(f"/{name}")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith(media_type)
    assert "max-age" in response.headers["cache-control"]
    assert response.content


async def test_home_page_links_the_icons(client: httpx.AsyncClient) -> None:
    page = await client.get("/")
    assert page.status_code == 200
    assert "rel='apple-touch-icon' href='/apple-touch-icon.png'" in page.text


async def test_initialize_advertises_icons(client: httpx.AsyncClient, tokens: Tokens) -> None:
    response = await McpSession(client, tokens.access_token).rpc(
        "initialize",
        {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "t", "version": "0"},
        },
    )
    icons = response.json()["result"]["serverInfo"]["icons"]
    assert {
        "src": f"{BASE_URL}/favicon.svg",
        "mimeType": "image/svg+xml",
        "sizes": ["any"],
    } in icons


def test_icon_dir_overrides_single_files(tmp_path: Path) -> None:
    (tmp_path / "favicon.svg").write_bytes(b"<svg>custom</svg>")
    icons = load_icons(tmp_path)
    assert icons["favicon.svg"] == b"<svg>custom</svg>"
    assert icons["apple-touch-icon.png"].startswith(b"\x89PNG")  # bundled default
    assert load_icons(None)["favicon.ico"] == icons["favicon.ico"]
