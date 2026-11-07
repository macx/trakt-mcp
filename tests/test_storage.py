import stat
from collections.abc import Iterator
from pathlib import Path

import pytest

from trakt_mcp.auth.pages import _age
from trakt_mcp.storage import Grant, Store, TraktTokens, hash_token


class Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def store(tmp_path: Path, clock: Clock) -> Iterator[Store]:
    db = Store(tmp_path / "sub" / "db.sqlite3", clock=clock)
    db.open()
    yield db
    db.close()


def grant(kind: str = "code", family: str = "fam", expires_at: int = 1_000_100) -> Grant:
    return Grant(kind, "client", "alice", {"scopes": ["trakt"]}, family, expires_at)


def test_database_file_is_private(tmp_path: Path, store: Store) -> None:
    mode = stat.S_IMODE((tmp_path / "sub" / "db.sqlite3").stat().st_mode)
    assert mode == 0o600


async def test_unopened_store_raises(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="not open"):
        await Store(tmp_path / "x.db").count_clients()


async def test_tokens_are_stored_hashed(tmp_path: Path, store: Store) -> None:
    await store.save_grant("super-secret-token", grant())
    raw = (tmp_path / "sub" / "db.sqlite3").read_bytes()
    wal = tmp_path / "sub" / "db.sqlite3-wal"
    content = raw + (wal.read_bytes() if wal.exists() else b"")
    assert b"super-secret-token" not in content
    assert hash_token("super-secret-token").encode() in content


async def test_clients_roundtrip(store: Store) -> None:
    assert await store.get_client("c1") is None
    await store.save_client("c1", {"client_id": "c1"})
    assert await store.get_client("c1") == {"client_id": "c1"}
    assert await store.count_clients() == 1


async def test_pending_login_is_single_use(store: Store) -> None:
    await store.save_pending_login("s", "c", {"a": 1}, ttl=60, max_per_client=5, max_total=10)
    assert await store.pop_pending_login("s") == {"a": 1}
    assert await store.pop_pending_login("s") is None


async def test_pending_login_expires(store: Store, clock: Clock) -> None:
    await store.save_pending_login("s", "c", {"a": 1}, ttl=60, max_per_client=5, max_total=10)
    clock.now += 61
    assert await store.pop_pending_login("s") is None


async def test_consume_grant_is_single_use(store: Store) -> None:
    await store.save_grant("code", grant())
    assert (await store.consume_grant("code", "code")) is not None
    assert await store.consume_grant("code", "code") is None


async def test_expired_grants_are_invisible(store: Store, clock: Clock) -> None:
    await store.save_grant("code", grant())
    clock.now += 101
    assert await store.get_grant("code", "code") is None
    assert await store.consume_grant("code", "code") is None


async def test_grant_kind_must_match(store: Store) -> None:
    await store.save_grant("tok", grant(kind="refresh"))
    assert await store.get_grant("tok", "access") is None
    assert await store.get_grant("tok", "refresh") is not None


async def test_retire_refresh_token(store: Store) -> None:
    await store.save_grant("r", grant(kind="refresh"))
    retired = await store.retire_refresh_token("r")
    assert retired is not None and retired.family == "fam"
    assert await store.retire_refresh_token("r") is None
    assert await store.get_grant("r", "refresh_used") is not None


async def test_revoke_family_and_subject(store: Store) -> None:
    await store.save_grant("a", grant(kind="access", family="f1"))
    await store.save_grant("b", grant(kind="refresh", family="f1"))
    await store.save_grant("c", grant(kind="access", family="f2"))
    assert await store.family_of("a") == "f1"
    await store.revoke_family("f1")
    assert await store.family_of("a") is None
    assert await store.family_of("b") is None
    assert await store.family_of("c") == "f2"
    await store.revoke_subject("alice")
    assert await store.family_of("c") is None


async def test_purge_expired(store: Store, clock: Clock) -> None:
    await store.save_grant("a", grant(kind="access"))
    await store.save_pending_login("s", "c", {}, ttl=10, max_per_client=5, max_total=10)
    clock.now += 1000
    await store.purge_expired(unapproved_client_ttl=3600)
    assert await store.family_of("a") is None


async def test_trakt_tokens_upsert_and_delete(store: Store) -> None:
    assert await store.get_trakt_tokens("alice") is None
    await store.save_trakt_tokens("alice", TraktTokens("a1", "r1", 5))
    await store.save_trakt_tokens("alice", TraktTokens("a2", "r2", 6))
    assert await store.get_trakt_tokens("alice") == TraktTokens("a2", "r2", 6)
    await store.delete_trakt_tokens("alice")
    assert await store.get_trakt_tokens("alice") is None


async def test_in_memory_store() -> None:
    db = Store(Path(":memory:"))
    db.open()
    assert await db.count_clients() == 0
    db.close()
    db.close()  # idempotent


async def test_purge_drops_only_stale_unapproved_clients(store: Store, clock: Clock) -> None:
    await store.save_client("stale", {})
    await store.save_client("approved", {})
    await store.approve_client("approved", "alice")
    clock.now += 10
    await store.save_client("fresh", {})
    await store.purge_expired(unapproved_client_ttl=5)
    assert await store.get_client("stale") is None
    assert await store.get_client("approved") is not None
    assert await store.get_client("fresh") is not None
    assert await store.client_created_at("missing") is None


async def test_approval_is_per_user(store: Store) -> None:
    await store.save_client("c", {})
    await store.approve_client("c", "bob")
    await store.approve_client("c", "bob")  # idempotent
    assert await store.client_approved("c", "bob")
    assert not await store.client_approved("c", "alice")
    assert await store.count_clients(approved=True) == 1
    assert await store.count_clients(approved=False) == 0


async def test_evict_unapproved_clients_keeps_newest_and_approved(
    store: Store, clock: Clock
) -> None:
    for name in ("a", "b", "c", "d"):
        clock.now += 1
        await store.save_client(name, {})
    await store.approve_client("a", "alice")
    await store.evict_unapproved_clients(keep=2)
    remaining = [n for n in "abcd" if await store.get_client(n) is not None]
    assert remaining == ["a", "c", "d"]


async def test_pending_logins_are_capped_by_evicting_oldest(store: Store, clock: Clock) -> None:
    for i in range(4):
        clock.now += 1
        await store.save_pending_login(
            f"x{i}", "c1", {"i": i}, ttl=60, max_per_client=2, max_total=10
        )
    assert await store.get_pending_login("x0") is None
    assert await store.get_pending_login("x3") == {"i": 3}
    for i in range(3):
        clock.now += 1
        await store.save_pending_login(
            f"y{i}", f"other{i}", {}, ttl=60, max_per_client=2, max_total=3
        )
    assert await store.get_pending_login("x2") is None  # globally oldest evicted
    assert await store.get_pending_login("y2") == {}


async def test_pending_login_update(store: Store) -> None:
    await store.save_pending_login("s", "c", {"a": 1}, ttl=60, max_per_client=5, max_total=10)
    await store.update_pending_login("s", {"a": 2})
    assert await store.get_pending_login("s") == {"a": 2}


async def test_retired_refresh_token_records_when(store: Store, clock: Clock) -> None:
    await store.save_grant("r", grant(kind="refresh"))
    retired = await store.retire_refresh_token("r")
    assert retired is not None and retired.used_at == int(clock.now)


def test_age_formatting() -> None:
    assert [_age(n) for n in (5, 600, 3 * 3600, 5 * 86400)] == [
        "just now",
        "10 minutes ago",
        "3 hours ago",
        "5 days ago",
    ]


async def test_settings_roundtrip(store: Store) -> None:
    assert await store.get_setting("k") is None
    await store.set_setting("k", "v1")
    await store.set_setting("k", "v2")
    assert await store.get_setting("k") == "v2"


async def test_failures_expire(store: Store, clock: Clock) -> None:
    await store.record_failure()
    await store.record_failure()
    assert await store.count_recent_failures(window=60) == 2
    clock.now += 61
    assert await store.count_recent_failures(window=60) == 0
