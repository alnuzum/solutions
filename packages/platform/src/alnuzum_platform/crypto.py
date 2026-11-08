"""AES-256-GCM envelopes for CRM secrets.

Envelope: ``b"v1"`` | key-id length (1 byte) | key id | nonce (12 bytes) | ciphertext+tag.
The associated data binds an envelope to its row (connection id and CRM), so a ciphertext copied
into another row does not decrypt. Keys come from the environment as
``SECRET_KEYS="k2:<base64>,k1:<base64>"``: the first key encrypts, every listed key decrypts.
"""

import base64
import binascii
import os
import re

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

VERSION = b"v1"
_KID = re.compile(r"^[A-Za-z0-9_-]{1,32}$")


class SecretError(Exception):
    """A secret cannot be decrypted: unknown key, tampered data or wrong row."""


class Keyring:
    def __init__(self, keys: list[tuple[str, bytes]]) -> None:
        if not keys:
            raise ValueError("at least one encryption key is required")
        ids = [kid for kid, _ in keys]
        if len(set(ids)) != len(ids):
            raise ValueError("key ids must be unique")
        for kid, key in keys:
            if not _KID.match(kid):
                raise ValueError("a key id is 1-32 letters, digits, - or _")
            if len(key) != 32:
                raise ValueError(f"key {kid} must be 32 bytes")
        self._keys = dict(keys)
        self.primary = ids[0]

    def __repr__(self) -> str:
        return f"Keyring(primary={self.primary!r}, keys={len(self._keys)})"

    @classmethod
    def from_env(cls, value: str) -> "Keyring":
        keys: list[tuple[str, bytes]] = []
        for item in filter(None, (part.strip() for part in value.split(","))):
            kid, sep, b64 = item.partition(":")
            if not sep:
                raise ValueError("SECRET_KEYS items look like key_id:base64")
            try:
                keys.append((kid.strip(), base64.b64decode(b64.strip(), validate=True)))
            except binascii.Error:
                raise ValueError(f"key {kid.strip()} is not valid base64") from None
        return cls(keys)

    def encrypt(self, plaintext: bytes, aad: bytes) -> bytes:
        kid = self.primary.encode()
        nonce = os.urandom(12)
        return (
            VERSION
            + bytes([len(kid)])
            + kid
            + nonce
            + AESGCM(self._keys[self.primary]).encrypt(nonce, plaintext, aad)
        )

    @staticmethod
    def key_id(envelope: bytes) -> str:
        if envelope[:2] != VERSION or len(envelope) < 3:
            raise SecretError("unknown envelope version")
        n = envelope[2]
        return envelope[3 : 3 + n].decode()

    def decrypt(self, envelope: bytes, aad: bytes) -> bytes:
        kid = self.key_id(envelope)
        key = self._keys.get(kid)
        if key is None:
            raise SecretError(f"no key {kid!r} in SECRET_KEYS")
        start = 3 + len(kid)
        nonce, body = envelope[start : start + 12], envelope[start + 12 :]
        try:
            return AESGCM(key).decrypt(nonce, body, aad)
        except InvalidTag:
            raise SecretError("secret does not decrypt: tampered or moved to another row") from None
