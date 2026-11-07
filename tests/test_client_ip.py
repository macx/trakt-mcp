import ipaddress
from pathlib import Path

from starlette.requests import Request

from tests.conftest import make_settings
from trakt_mcp.auth.provider import _client_ip


def request(peer: str, forwarded: str | None = None) -> Request:
    headers = [(b"x-forwarded-for", forwarded.encode())] if forwarded else []
    return Request({"type": "http", "headers": headers, "client": (peer, 1234)})


def test_without_trusted_proxies_the_peer_counts(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    assert _client_ip(request("172.18.0.5", "1.2.3.4"), settings) == "172.18.0.5"


def test_forwarded_for_is_used_only_through_trusted_proxies(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    settings.trusted_proxies = [ipaddress.ip_network("172.18.0.0/16")]
    # The client may prepend anything; the hop the trusted proxy added is the right one.
    assert _client_ip(request("172.18.0.5", "6.6.6.6, 1.2.3.4"), settings) == "1.2.3.4"
    assert _client_ip(request("9.9.9.9", "1.2.3.4"), settings) == "9.9.9.9"


def test_garbage_and_missing_values(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    settings.trusted_proxies = [ipaddress.ip_network("0.0.0.0/0")]
    assert _client_ip(request("172.18.0.5", "not-an-ip"), settings) == "not-an-ip"
    assert _client_ip(request("172.18.0.5"), settings) == ""
    no_client = Request({"type": "http", "headers": [], "client": None})
    assert _client_ip(no_client, make_settings(tmp_path)) == ""


def test_ipv6_clients_are_grouped_by_64(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    first = _client_ip(request("2001:db8:1:2::1"), settings)
    second = _client_ip(request("2001:db8:1:2:ffff::9"), settings)
    assert first == second == "2001:db8:1:2::/64"
