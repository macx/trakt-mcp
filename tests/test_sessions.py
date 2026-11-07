import asyncio
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from tests.fake_trakt import CLIENT_ID, FakeTrakt
from trakt_mcp.storage import Grant, Store, TraktTokens
from trakt_mcp.trakt.errors import TraktAuthError, TraktForbiddenError
from trakt_mcp.trakt.http import TraktOAuth
from trakt_mcp.trakt.sessions import REFRESH_MARGIN_SECONDS, ReconnectRequiredError, TraktSessions


class Clock:
    now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def store(tmp_path: Path, clock: Clock) -> Iterator[Store]:
    db = Store(tmp_path / "db", clock=clock)
    db.open()
    yield db
    db.close()


@pytest.fixture
def fake() -> FakeTrakt:
    return FakeTrakt()


@pytest.fixture
def sessions(store: Store, fake: FakeTrakt) -> TraktSessions:
    oauth = TraktOAuth(
        httpx.AsyncClient(transport=fake.transport()),
        auth_url="https://auth.trakt.tv",
        client_id=CLIENT_ID,
        client_secret=None,
        redirect_uri="https://x/trakt/callback",
    )
    return TraktSessions(store, oauth)


async def seed(store: Store, fake: FakeTrakt, expires_in: int) -> None:
    fake.access_tokens.add("old-access")
    fake.refresh_tokens.add("old-refresh")
    await store.save_trakt_tokens(
        "alice", TraktTokens("old-access", "old-refresh", store.now() + expires_in)
    )


async def test_valid_token_is_returned_without_refresh(
    sessions: TraktSessions, store: Store, fake: FakeTrakt
) -> None:
    await seed(store, fake, expires_in=3600)
    assert await sessions.access_token("alice") == "old-access"
    assert fake.refresh_calls == 0


async def test_expiring_token_is_refreshed_and_saved(
    sessions: TraktSessions, store: Store, fake: FakeTrakt
) -> None:
    await seed(store, fake, expires_in=REFRESH_MARGIN_SECONDS - 1)
    token = await sessions.access_token("alice")
    assert token != "old-access"
    stored = await store.get_trakt_tokens("alice")
    assert stored is not None and stored.access_token == token
    assert stored.refresh_token != "old-refresh"


async def test_concurrent_calls_refresh_only_once(
    sessions: TraktSessions, store: Store, fake: FakeTrakt
) -> None:
    """Refresh tokens are single-use; a second parallel refresh would lock the user out."""
    await seed(store, fake, expires_in=0)
    tokens = await asyncio.gather(*(sessions.access_token("alice") for _ in range(5)))
    assert len(set(tokens)) == 1
    assert fake.refresh_calls == 1


async def test_missing_tokens_require_reconnect(sessions: TraktSessions) -> None:
    with pytest.raises(ReconnectRequiredError, match="reconnect"):
        await sessions.access_token("alice")


async def test_rejected_refresh_disconnects_user(
    sessions: TraktSessions, store: Store, fake: FakeTrakt
) -> None:
    await seed(store, fake, expires_in=0)
    fake.refresh_tokens.clear()  # e.g. user revoked the app on trakt.tv
    await store.save_grant("our-token", Grant("access", "c", "alice", {}, "f", store.now() + 100))
    with pytest.raises(ReconnectRequiredError):
        await sessions.access_token("alice")
    assert await store.get_trakt_tokens("alice") is None
    assert await store.get_grant("our-token", "access") is None


async def test_call_retries_once_after_auth_error(
    sessions: TraktSessions, store: Store, fake: FakeTrakt
) -> None:
    await seed(store, fake, expires_in=3600)
    seen: list[str] = []

    async def fn(token: str) -> str:
        seen.append(token)
        if token == "old-access":
            raise TraktAuthError("revoked")
        return "ok"

    assert await sessions.call("alice", fn) == "ok"
    assert seen[0] == "old-access" and seen[1] != "old-access"


async def test_call_does_not_retry_reconnect_errors(sessions: TraktSessions) -> None:
    async def fn(token: str) -> str:  # pragma: no cover - never reached
        return token

    with pytest.raises(ReconnectRequiredError):
        await sessions.call("alice", fn)


async def test_call_propagates_reconnect_raised_by_fn(
    sessions: TraktSessions, store: Store, fake: FakeTrakt
) -> None:
    await seed(store, fake, expires_in=3600)

    async def fn(token: str) -> str:
        raise ReconnectRequiredError

    with pytest.raises(ReconnectRequiredError):
        await sessions.call("alice", fn)
    assert fake.refresh_calls == 0


async def test_forbidden_does_not_burn_the_refresh_token(
    sessions: TraktSessions, store: Store, fake: FakeTrakt
) -> None:
    """403 is not an auth failure; refreshing on it would risk the single-use token."""
    await seed(store, fake, expires_in=3600)

    async def fn(token: str) -> str:
        raise TraktForbiddenError("vip only", status=403)

    with pytest.raises(TraktForbiddenError):
        await sessions.call("alice", fn)
    assert fake.refresh_calls == 0
