"""Runtime configuration, read exclusively from environment variables.

Nothing in here has a secret default: the server refuses to start unless the
operator has explicitly configured the Trakt app and the allowed users.
"""

from __future__ import annotations

from ipaddress import IPv4Network, IPv6Network
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit

from pydantic import Field, HttpUrl, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

#: Redirect URIs accepted during dynamic client registration when the operator
#: does not configure their own list. Claude's hosted connectors use the first
#: two; loopback URIs (any port) cover local clients such as Claude Code.
DEFAULT_ALLOWED_REDIRECT_URIS = (
    "https://claude.ai/api/mcp/auth_callback",
    "https://claude.com/api/mcp/auth_callback",
    "http://localhost/*",
    "http://127.0.0.1/*",
)

_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def _split_csv(value: object) -> object:
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    return value


class Settings(BaseSettings):
    """All settings of a trakt-mcp instance."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- Trakt application -------------------------------------------------
    trakt_client_id: str = Field(min_length=1, description="Client ID of your Trakt app.")
    trakt_client_secret: SecretStr | None = Field(
        default=None,
        description="Optional; Trakt prefers PKCE, which this server always uses.",
    )
    trakt_redirect_uri: str = Field(
        default="http://localhost:8000/trakt/callback",
        description="A redirect URI registered for your Trakt app. Sign-in uses the device "
        "flow, but Trakt's token refresh expects a registered redirect_uri.",
    )
    trakt_api_url: HttpUrl = HttpUrl("https://api.trakt.tv")
    trakt_auth_url: HttpUrl = HttpUrl("https://auth.trakt.tv")

    # --- Access control ----------------------------------------------------
    allowed_trakt_users: Annotated[list[str], NoDecode] = Field(
        min_length=1,
        description="Trakt user slugs (as in the profile URL trakt.tv/users/<slug>) allowed "
        "to sign in. Everyone else is rejected.",
    )
    allowed_redirect_uris: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: list(DEFAULT_ALLOWED_REDIRECT_URIS),
        description="Redirect URIs MCP clients may register. A trailing '/*' allows any "
        "path below it; loopback hosts additionally match any port.",
    )
    max_registered_clients: int = Field(default=200, ge=1)
    trusted_proxies: Annotated[list[IPv4Network | IPv6Network], NoDecode] = Field(
        default_factory=list,
        description="Networks of reverse proxies whose X-Forwarded-For is trusted for "
        "per-IP rate limiting (e.g. the Docker network of Caddy). Empty: use the peer.",
    )

    # --- HTTP server -------------------------------------------------------
    public_url: HttpUrl = Field(description="External base URL, e.g. https://m1.example.com")
    host: str = Field(default="127.0.0.1", description="Bind address; the image sets 0.0.0.0.")
    port: int = Field(default=8000, ge=1, le=65535)
    log_level: str = "INFO"

    # --- Storage & token lifetimes -----------------------------------------
    data_dir: Path = Path("/data")
    icon_dir: Path | None = Field(
        default=None,
        description="Directory with favicon.svg, favicon.ico and/or apple-touch-icon.png "
        "to replace the bundled neutral icons.",
    )
    access_token_ttl_seconds: int = Field(default=3600, ge=60)
    refresh_token_ttl_seconds: int = Field(default=30 * 24 * 3600, ge=3600)
    authorization_code_ttl_seconds: int = Field(default=300, ge=30, le=600)
    pending_login_ttl_seconds: int = Field(default=600, ge=60, le=3600)
    max_session_seconds: int = Field(
        default=90 * 24 * 3600,
        ge=3600,
        description="Absolute lifetime of a sign-in; refreshing cannot extend it beyond this.",
    )
    unapproved_client_ttl_seconds: int = Field(
        default=3600,
        ge=60,
        description="Registered clients nobody approved are deleted after this.",
    )
    max_pending_logins: int = Field(default=1000, ge=1)

    @field_validator(
        "allowed_trakt_users", "allowed_redirect_uris", "trusted_proxies", mode="before"
    )
    @classmethod
    def _parse_csv(cls, value: object) -> object:
        return _split_csv(value)

    @field_validator("allowed_trakt_users")
    @classmethod
    def _normalise_users(cls, value: list[str]) -> list[str]:
        return [user.lower() for user in value]

    @field_validator("public_url")
    @classmethod
    def _require_https(cls, value: HttpUrl) -> HttpUrl:
        if value.scheme != "https" and value.host not in _LOOPBACK_HOSTS:
            raise ValueError("public_url must use https (plain http is only allowed for localhost)")
        if value.path not in (None, "", "/"):
            raise ValueError("public_url must not contain a path")
        return value

    @property
    def base_url(self) -> str:
        """The public URL without a trailing slash."""
        return str(self.public_url).rstrip("/")

    @property
    def mcp_url(self) -> str:
        return f"{self.base_url}/mcp"

    @property
    def database_path(self) -> Path:
        return self.data_dir / "trakt-mcp.sqlite3"

    @property
    def public_host(self) -> str:
        """Host (with port, if any) as it appears in the HTTP Host header."""
        return urlsplit(self.base_url).netloc


def redirect_uri_allowed(uri: str, patterns: list[str]) -> bool:
    """Return True if ``uri`` matches one of the configured redirect URI patterns.

    Matching is deliberately strict: scheme and host must match exactly, query
    strings and fragments are never allowed, and only a trailing ``/*`` acts as
    a wildcard (for the path). Loopback hosts additionally match any port, as
    native clients pick a random port (RFC 8252, section 7.3).
    """
    candidate = urlsplit(uri)
    if candidate.query or candidate.fragment or candidate.username or candidate.password:
        return False
    for pattern in patterns:
        wildcard = pattern.endswith("/*")
        expected = urlsplit(pattern.removesuffix("*") if wildcard else pattern)
        if candidate.scheme != expected.scheme or candidate.hostname != expected.hostname:
            continue
        is_loopback = expected.hostname in _LOOPBACK_HOSTS
        if not is_loopback and candidate.port != expected.port:
            continue
        if wildcard:
            if (candidate.path or "/").startswith(expected.path or "/"):
                return True
        elif candidate.path == expected.path:
            return True
    return False
