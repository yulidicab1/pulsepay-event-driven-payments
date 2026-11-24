"""Helpers for API key generation and hashing.

API keys are shown to the merchant exactly once, at creation time. Only a
SHA-256 hash is persisted, so a database leak does not expose usable keys.
"""

import hashlib
import secrets

from pwdlib import PasswordHash
from pwdlib.exceptions import UnknownHashError

password_hasher = PasswordHash.recommended()
dummy_password_hash = password_hasher.hash(secrets.token_urlsafe(32))

API_KEY_PREFIX = "sk_test_"
# Number of leading characters kept as a non-secret identifier for dashboards.
DISPLAY_PREFIX_LENGTH = 12


def generate_api_key() -> str:
    """Return a new random secret API key, including its prefix."""
    return f"{API_KEY_PREFIX}{secrets.token_urlsafe(32)}"


def hash_api_key(raw_key: str) -> str:
    """Return the SHA-256 hex digest stored for a raw API key."""
    return hashlib.sha256(raw_key.encode()).hexdigest()


def key_display_prefix(raw_key: str) -> str:
    """Return the short, non-secret prefix used to identify a key."""
    return raw_key[:DISPLAY_PREFIX_LENGTH]


def hash_password(password: str) -> str:
    if not 15 <= len(password) <= 128:
        raise ValueError("Passwords must contain 15 to 128 characters.")
    return password_hasher.hash(password)


def verify_password(password: str, encoded: str) -> bool:
    try:
        return password_hasher.verify(password, encoded)
    except (UnknownHashError, ValueError):
        return False
