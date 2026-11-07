from pathlib import Path

import pytest
from pydantic import ValidationError

from tests.conftest import make_settings
from trakt_mcp.config import DEFAULT_ALLOWED_REDIRECT_URIS, redirect_uri_allowed

DEFAULTS = list(DEFAULT_ALLOWED_REDIRECT_URIS)


class TestSettings:
    def test_csv_lists_are_parsed_and_users_lowercased(self, tmp_path: Path) -> None:
        settings = make_settings(tmp_path, allowed_trakt_users=" Alice , bob ,")
        assert settings.allowed_trakt_users == ["alice", "bob"]

    def test_derived_urls(self, tmp_path: Path) -> None:
        settings = make_settings(tmp_path, public_url="https://m1.example.com/")
        assert settings.base_url == "https://m1.example.com"
        assert settings.mcp_url == "https://m1.example.com/mcp"
        assert settings.public_host == "m1.example.com"
        assert settings.database_path == tmp_path / "trakt-mcp.sqlite3"

    def test_plain_http_is_rejected_for_public_hosts(self, tmp_path: Path) -> None:
        with pytest.raises(ValidationError, match="https"):
            make_settings(tmp_path, public_url="http://m1.example.com")

    def test_plain_http_is_allowed_for_localhost(self, tmp_path: Path) -> None:
        assert make_settings(tmp_path, public_url="http://localhost:8000").public_host == (
            "localhost:8000"
        )

    def test_public_url_must_not_have_a_path(self, tmp_path: Path) -> None:
        with pytest.raises(ValidationError, match="path"):
            make_settings(tmp_path, public_url="https://example.com/trakt")

    def test_allowed_users_are_required(self, tmp_path: Path) -> None:
        with pytest.raises(ValidationError):
            make_settings(tmp_path, allowed_trakt_users="")

    def test_client_secret_is_hidden_in_repr(self, tmp_path: Path) -> None:
        settings = make_settings(tmp_path, trakt_client_secret="hunter2")
        assert "hunter2" not in repr(settings)


class TestRedirectUriAllowlist:
    @pytest.mark.parametrize(
        "uri",
        [
            "https://claude.ai/api/mcp/auth_callback",
            "https://claude.com/api/mcp/auth_callback",
            "http://localhost:33418/callback",
            "http://127.0.0.1:9999/oauth/cb",
            "http://localhost/callback",
        ],
    )
    def test_accepts(self, uri: str) -> None:
        assert redirect_uri_allowed(uri, DEFAULTS)

    @pytest.mark.parametrize(
        "uri",
        [
            "https://evil.example/api/mcp/auth_callback",
            "https://claude.ai.evil.example/api/mcp/auth_callback",
            "https://claude.ai/api/mcp/auth_callback/extra",
            "https://claude.ai/api/mcp/auth_callback?x=1",
            "https://claude.ai/api/mcp/auth_callback#frag",
            "http://claude.ai/api/mcp/auth_callback",
            "https://claude.ai:8443/api/mcp/auth_callback",
            "https://user@claude.ai/api/mcp/auth_callback",
            "https://localhost:1234/callback",
            "http://localhost.evil.example/callback",
            "javascript:alert(1)",
        ],
    )
    def test_rejects(self, uri: str) -> None:
        assert not redirect_uri_allowed(uri, DEFAULTS)

    def test_path_wildcard_on_public_host(self) -> None:
        patterns = ["https://gemini.google.com/oauth/*"]
        assert redirect_uri_allowed("https://gemini.google.com/oauth/cb", patterns)
        assert not redirect_uri_allowed("https://gemini.google.com/other", patterns)


def test_trusted_proxies_are_parsed(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, trusted_proxies="172.18.0.0/16, ::1/128")
    assert [str(n) for n in settings.trusted_proxies] == ["172.18.0.0/16", "::1/128"]
