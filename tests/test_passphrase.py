import pytest

from trakt_mcp.auth.passphrase import hash_passphrase, verify_passphrase


def test_roundtrip_and_salting() -> None:
    first, second = hash_passphrase("twelve chars!"), hash_passphrase("twelve chars!")
    assert first != second  # random salt
    assert verify_passphrase("twelve chars!", first)
    assert verify_passphrase("twelve chars!", second)
    assert not verify_passphrase("twelve chars?", first)
    assert "twelve" not in first


def test_minimum_length() -> None:
    with pytest.raises(ValueError, match="at least 12"):
        hash_passphrase("too short")


@pytest.mark.parametrize("stored", ["", "garbage", "bcrypt$1$2$3$a$b", "scrypt$x$8$1$AA$AA"])
def test_malformed_hashes_never_verify(stored: str) -> None:
    assert not verify_passphrase("anything at all", stored)
