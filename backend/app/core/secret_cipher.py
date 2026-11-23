"""AES-256-GCM encryption for secrets bound to a merchant and endpoint.

The external JSON key file contains an "active_key_id" and a "keys" object
mapping key identifiers to standard Base64-encoded 32-byte keys.

AAD format version 1 uses the following exact byte sequence.
    b"PulsePay/webhook-secret/v1\\0" + key_id.encode("ascii") + b"\\0"
    + merchant_id.bytes + endpoint_id.bytes
UUIDs use their 16-byte, big-endian representation. Key identifiers contain
only ASCII letters, digits, underscores, dots, and hyphens.

Every encryption draws a fresh 96-bit random nonce. The envelope exposes its
key identifier and nonce for a future persistence-layer uniqueness constraint.
Provision the key file outside the repository and mount it only at runtime.
"""

import base64
import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from cryptography.exceptions import InvalidTag, UnsupportedAlgorithm
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class SecretCipherError(Exception):
    """Encryption could not complete safely."""


class SecretConfigurationError(SecretCipherError):
    """The configured encryption keys are unavailable or invalid."""


class SecretDecryptionError(SecretCipherError):
    """A secret could not be authenticated and decrypted."""


@dataclass(frozen=True, repr=False)
class EncryptedSecret:
    key_id: str
    nonce: bytes
    ciphertext: bytes
    version: int = 1


def _unique_json_fields(pairs: list[tuple[str, object]]) -> dict[str, object]:
    fields = {}
    for name, value in pairs:
        if name in fields:
            raise ValueError("Duplicate encryption key file field")
        fields[name] = value
    return fields


class SecretCipher:
    def __init__(self, *, active_key_id: str, keys: Mapping[str, bytes]) -> None:
        if not isinstance(keys, Mapping) or not keys:
            raise SecretConfigurationError("Encryption keys are required.")
        if not isinstance(active_key_id, str) or active_key_id not in keys:
            raise SecretConfigurationError("An active encryption key is required.")
        if any(
            not isinstance(key_id, str)
            or re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", key_id) is None
            or not isinstance(key, bytes)
            or len(key) != 32
            for key_id, key in keys.items()
        ):
            raise SecretConfigurationError("Encryption key configuration is invalid.")
        self._active_key_id = active_key_id
        try:
            self._ciphers = {key_id: AESGCM(key) for key_id, key in keys.items()}
        except UnsupportedAlgorithm:
            raise SecretConfigurationError("AES-256-GCM is unavailable.") from None

    @classmethod
    def from_key_file(cls, path: Path | None) -> "SecretCipher":
        if path is None:
            raise SecretConfigurationError("An encryption key file is required.")
        try:
            document = json.loads(
                Path(path).read_text(encoding="utf-8"), object_pairs_hook=_unique_json_fields
            )
            if not isinstance(document, dict) or set(document) != {"active_key_id", "keys"}:
                raise ValueError("Invalid encryption key file structure")
            if not isinstance(document["keys"], dict):
                raise ValueError("Invalid encryption key mapping")
            keys = {
                key_id: base64.b64decode(encoded_key, validate=True)
                for key_id, encoded_key in document["keys"].items()
            }
        except (OSError, ValueError, TypeError):
            raise SecretConfigurationError("Encryption key file is unavailable or invalid.") from None
        return cls(active_key_id=document["active_key_id"], keys=keys)

    @staticmethod
    def _associated_data(key_id: str, merchant_id: UUID, endpoint_id: UUID) -> bytes:
        if not isinstance(merchant_id, UUID) or not isinstance(endpoint_id, UUID):
            raise ValueError("Secret context requires UUID identifiers")
        return (
            b"PulsePay/webhook-secret/v1\0"
            + key_id.encode("ascii")
            + b"\0"
            + merchant_id.bytes
            + endpoint_id.bytes
        )

    def encrypt(self, plaintext: str, *, merchant_id: UUID, endpoint_id: UUID) -> EncryptedSecret:
        if not isinstance(plaintext, str):
            raise SecretCipherError("Unable to encrypt secret.")
        try:
            nonce = os.urandom(12)
            if not isinstance(nonce, bytes) or len(nonce) != 12:
                raise ValueError("Invalid nonce")
            associated_data = self._associated_data(self._active_key_id, merchant_id, endpoint_id)
            ciphertext = self._ciphers[self._active_key_id].encrypt(
                nonce, plaintext.encode("utf-8"), associated_data
            )
        except (OSError, ValueError, TypeError, OverflowError):
            raise SecretCipherError("Unable to encrypt secret.") from None
        return EncryptedSecret(key_id=self._active_key_id, nonce=nonce, ciphertext=ciphertext)

    def decrypt(self, encrypted: EncryptedSecret, *, merchant_id: UUID, endpoint_id: UUID) -> str:
        if (
            not isinstance(encrypted, EncryptedSecret)
            or type(encrypted.version) is not int
            or encrypted.version != 1
            or not isinstance(encrypted.key_id, str)
            or encrypted.key_id not in self._ciphers
            or not isinstance(encrypted.nonce, bytes)
            or len(encrypted.nonce) != 12
            or not isinstance(encrypted.ciphertext, bytes)
            or len(encrypted.ciphertext) < 16
        ):
            raise SecretDecryptionError("Unable to decrypt secret.")
        try:
            associated_data = self._associated_data(encrypted.key_id, merchant_id, endpoint_id)
            plaintext = self._ciphers[encrypted.key_id].decrypt(
                encrypted.nonce, encrypted.ciphertext, associated_data
            )
            return plaintext.decode("utf-8")
        except (InvalidTag, ValueError, TypeError, OverflowError):
            raise SecretDecryptionError("Unable to decrypt secret.") from None
