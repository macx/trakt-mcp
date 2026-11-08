"""Application wiring: settings -> store -> Trakt clients -> auth -> MCP app."""

from __future__ import annotations

import contextlib
import json
import logging
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any

import httpx
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp_types import CallToolResult, InputRequiredResult, TextContent, Tool
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from trakt_mcp import __version__
from trakt_mcp.auth.provider import SCOPE, TraktAuthProvider
from trakt_mcp.config import Settings
from trakt_mcp.icons import home_page, icon_endpoint, load_icons, mcp_icons
from trakt_mcp.schema import compact_schema
from trakt_mcp.storage import Store
from trakt_mcp.tools import SERVER_INSTRUCTIONS, register_tools
from trakt_mcp.trakt.http import TraktApi, TraktOAuth, new_http_client
from trakt_mcp.trakt.service import TraktService
from trakt_mcp.trakt.sessions import TraktSessions

logger = logging.getLogger(__name__)


class LeanMCPServer(MCPServer):
    """An MCP server that spends as little of the assistant's context as possible.

    - The tool list carries compacted schemas (see :mod:`trakt_mcp.schema`).
    - A structured result's text copy (sent alongside ``structuredContent`` for
      clients that only read text) is compact JSON instead of the SDK's
      indented JSON; it holds exactly the same data.
    """

    async def call_tool(
        self, name: str, arguments: dict[str, Any], context: Any = None
    ) -> CallToolResult | InputRequiredResult:
        result = await super().call_tool(name, arguments, context)
        if isinstance(result, CallToolResult) and result.structured_content is not None:
            text = json.dumps(result.structured_content, ensure_ascii=False, separators=(",", ":"))
            result = result.model_copy(update={"content": [TextContent(type="text", text=text)]})
        return result

    async def list_tools(self) -> list[Tool]:
        return [
            tool.model_copy(
                update={
                    "input_schema": compact_schema(tool.input_schema),
                    "output_schema": (
                        compact_schema(tool.output_schema, output=True)
                        if tool.output_schema
                        else None
                    ),
                }
            )
            for tool in await super().list_tools()
        ]


_SECURITY_HEADERS = (
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"no-referrer"),
    (b"x-frame-options", b"DENY"),
    (b"content-security-policy", b"default-src 'none'; style-src 'unsafe-inline'"),
)


@dataclass
class App:
    """The ASGI application plus the resources it owns (exposed for tests)."""

    asgi: Starlette
    store: Store
    http: httpx.AsyncClient
    provider: TraktAuthProvider


def build_app(
    settings: Settings,
    *,
    http: httpx.AsyncClient | None = None,
    clock: Callable[[], float] = time.time,
) -> App:
    store = Store(settings.database_path, clock=clock)
    http_client = http or new_http_client()
    oauth = TraktOAuth(
        http_client,
        auth_url=str(settings.trakt_auth_url),
        client_id=settings.trakt_client_id,
        client_secret=(
            settings.trakt_client_secret.get_secret_value()
            if settings.trakt_client_secret
            else None
        ),
        redirect_uri=settings.trakt_redirect_uri,
    )
    api = TraktApi(
        http_client, api_url=str(settings.trakt_api_url), client_id=settings.trakt_client_id
    )
    sessions = TraktSessions(store, oauth)
    provider = TraktAuthProvider(settings, store, oauth, api, sessions)

    server = LeanMCPServer(
        name="trakt-mcp",
        title="Trakt",
        version=__version__,
        instructions=SERVER_INSTRUCTIONS,
        website_url="https://github.com/macx/trakt-mcp",
        icons=mcp_icons(settings.base_url),
        auth_server_provider=provider,
        auth=AuthSettings(
            issuer_url=settings.base_url,
            resource_server_url=settings.mcp_url,
            validate_token_resource=True,
            required_scopes=[SCOPE],
            client_registration_options=ClientRegistrationOptions(
                enabled=True, valid_scopes=[SCOPE], default_scopes=[SCOPE]
            ),
            revocation_options=RevocationOptions(enabled=True),
        ),
        log_level=settings.log_level.upper(),  # type: ignore[arg-type]
    )
    register_tools(server, lambda subject: TraktService(api, sessions, subject))

    async def login(request: Request) -> Response:
        return await provider.handle_login(request)

    async def consent(request: Request) -> Response:
        return await provider.handle_consent_decision(request)

    async def home(_: Request) -> Response:
        return home_page("trakt-mcp")

    async def healthz(_: Request) -> Response:
        return JSONResponse({"status": "ok"})

    # Custom routes are public by design: they are the sign-in flow itself, and
    # the health check is used by the container runtime.
    server.custom_route("/login", methods=["GET", "POST"], include_in_schema=False)(login)
    server.custom_route("/consent", methods=["POST"], include_in_schema=False)(consent)
    server.custom_route("/healthz", methods=["GET"], include_in_schema=False)(healthz)
    server.custom_route("/", methods=["GET"], include_in_schema=False)(home)
    for name, content in load_icons(settings.icon_dir).items():
        server.custom_route(f"/{name}", methods=["GET"], include_in_schema=False)(
            icon_endpoint(name, content)
        )

    asgi = server.streamable_http_app(
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[settings.public_host, "localhost:*", "127.0.0.1:*"],
            allowed_origins=[settings.base_url, "https://claude.ai", "https://claude.com"],
        ),
    )

    inner_lifespan = asgi.router.lifespan_context

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        store.open()
        await store.purge_expired(settings.unapproved_client_ttl_seconds)
        logger.info("trakt-mcp %s listening for %s", __version__, settings.mcp_url)
        try:
            async with inner_lifespan(app):
                yield
        finally:
            await http_client.aclose()
            store.close()

    asgi.router.lifespan_context = lifespan
    hsts = settings.base_url.startswith("https://")
    asgi.add_middleware(SecurityHeadersMiddleware, hsts=hsts)
    return App(asgi=asgi, store=store, http=http_client, provider=provider)


class SecurityHeadersMiddleware:
    """Add conservative security headers to every HTTP response."""

    def __init__(self, app: ASGIApp, *, hsts: bool) -> None:
        self.app = app
        self.headers = _SECURITY_HEADERS + (
            ((b"strict-transport-security", b"max-age=31536000"),) if hsts else ()
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                present = {name.lower() for name, _ in headers}
                headers.extend(h for h in self.headers if h[0] not in present)
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_with_headers)
