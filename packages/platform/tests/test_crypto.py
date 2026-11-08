import base64

import pytest

from alnuzum_platform.crypto import Keyring, SecretError

K1, K2 = b"\x01" * 32, b"\x02" * 32


def test_roundtrip_and_ciphertext_hides_the_value() -> None:
    ring = Keyring([("k1", K1)])
    blob = ring.encrypt(b"super-secret-token", b"row-1")
    assert b"super-secret-token" not in blob
    assert ring.decrypt(blob, b"row-1") == b"super-secret-token"
    assert ring.encrypt(b"x", b"a") != ring.encrypt(b"x", b"a")  # fresh nonce every time


def test_ciphertext_moved_to_another_row_does_not_decrypt() -> None:
    ring = Keyring([("k1", K1)])
    blob = ring.encrypt(b"token", b"crm-connection:1:amocrm")
    with pytest.raises(SecretError, match="another row"):
        ring.decrypt(blob, b"crm-connection:2:amocrm")


def test_tampered_ciphertext_does_not_decrypt() -> None:
    ring = Keyring([("k1", K1)])
    blob = bytearray(ring.encrypt(b"token", b"a"))
    blob[-1] ^= 1
    with pytest.raises(SecretError):
        ring.decrypt(bytes(blob), b"a")


def test_rotation_new_key_encrypts_old_key_still_decrypts() -> None:
    old = Keyring([("k1", K1)])
    blob = old.encrypt(b"token", b"a")
    new = Keyring([("k2", K2), ("k1", K1)])
    assert new.decrypt(blob, b"a") == b"token"
    assert Keyring.key_id(new.encrypt(b"token", b"a")) == "k2"
    with pytest.raises(SecretError, match="no key"):
        Keyring([("k2", K2)]).decrypt(blob, b"a")


def test_unknown_envelope_version() -> None:
    with pytest.raises(SecretError, match="version"):
        Keyring([("k1", K1)]).decrypt(b"v9xxxx", b"a")


def test_from_env() -> None:
    env = f"k2:{base64.b64encode(K2).decode()}, k1:{base64.b64encode(K1).decode()}"
    ring = Keyring.from_env(env)
    assert ring.primary == "k2"
    assert "AAAA" not in repr(ring)


@pytest.mark.parametrize(
    ("env", "error"),
    [
        ("", "at least one"),
        ("k1", "key_id:base64"),
        ("k1:not base64!", "base64"),
        (f"k1:{base64.b64encode(b'short').decode()}", "32 bytes"),
        (f"bad id:{base64.b64encode(K1).decode()}", "key id"),
        (f"k1:{base64.b64encode(K1).decode()},k1:{base64.b64encode(K2).decode()}", "unique"),
    ],
)
def test_from_env_refuses_bad_keys(env: str, error: str) -> None:
    with pytest.raises(ValueError, match=error):
        Keyring.from_env(env)
