"""Unit tests with synthetic keys and temporary key files."""

import base64
import json
import traceback
from dataclasses import replace
from uuid import UUID

import pytest
from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.core.config import Settings
from app.core.secret_cipher import (
    SecretCipher,
    SecretCipherError,
    SecretConfigurationError,
    SecretDecryptionError,
)

MERCHANT_ID = UUID("00000000-0000-4000-8000-000000000001")
ENDPOINT_ID = UUID("00000000-0000-4000-8000-000000000002")
OTHER_ID = UUID("00000000-0000-4000-8000-000000000003")
CONTEXT = {"merchant_id": MERCHANT_ID, "endpoint_id": ENDPOINT_ID}
KEYS = {"key-1": bytes(range(32)), "key-2": bytes(range(32, 64))}


@pytest.fixture(autouse=True)
def reset_database():
    """Override the parent database fixture for these independent unit tests."""
    return None


@pytest.fixture
def cipher():
    return SecretCipher(active_key_id="key-1", keys=KEYS)


def write_key_file(path, *, active_key_id="key-1", keys=KEYS):
    path.write_text(
        json.dumps(
            {
                "active_key_id": active_key_id,
                "keys": {
                    key_id: base64.b64encode(key).decode("ascii")
                    for key_id, key in keys.items()
                },
            }
        ),
        encoding="utf-8",
    )


@pytest.mark.parametrize("plaintext", ["", "synthetic secret", "España 日本語 🔐", "null\0byte", "a" * 4096])
def test_encrypt_and_decrypt_preserve_plaintext(cipher, plaintext):
    encrypted = cipher.encrypt(plaintext, **CONTEXT)
    assert encrypted.key_id == "key-1"
    assert encrypted.version == 1
    assert cipher.decrypt(encrypted, **CONTEXT) == plaintext


def test_associated_data_has_documented_stable_representation(cipher):
    encrypted = cipher.encrypt("synthetic secret", **CONTEXT)
    expected_aad = (
        b"PulsePay/webhook-secret/v1\0key-1\0"
        + MERCHANT_ID.bytes
        + ENDPOINT_ID.bytes
    )
    assert AESGCM(KEYS["key-1"]).decrypt(
        encrypted.nonce, encrypted.ciphertext, expected_aad
    ) == b"synthetic secret"


def test_each_encryption_uses_a_fresh_96_bit_nonce(cipher):
    encrypted = [cipher.encrypt("same secret", **CONTEXT) for _ in range(128)]
    assert all(len(item.nonce) == 12 for item in encrypted)
    assert len({item.nonce for item in encrypted}) == len(encrypted)
    assert len({item.ciphertext for item in encrypted}) == len(encrypted)


def test_nonce_comes_from_system_randomness(cipher, monkeypatch):
    calls = []
    expected_nonce = bytes(range(12))

    def random_bytes(length):
        calls.append(length)
        return expected_nonce

    monkeypatch.setattr("app.core.secret_cipher.os.urandom", random_bytes)
    assert cipher.encrypt("synthetic secret", **CONTEXT).nonce == expected_nonce
    assert calls == [12]


def test_randomness_failure_is_controlled(cipher, monkeypatch):
    def unavailable(length):
        raise OSError("synthetic-sensitive-marker")

    monkeypatch.setattr("app.core.secret_cipher.os.urandom", unavailable)
    with pytest.raises(SecretCipherError) as error:
        cipher.encrypt("synthetic secret", **CONTEXT)
    assert "synthetic-sensitive-marker" not in "".join(traceback.format_exception(error.value))


def test_invalid_nonce_from_random_source_is_rejected(cipher, monkeypatch):
    monkeypatch.setattr("app.core.secret_cipher.os.urandom", lambda length: b"short")
    with pytest.raises(SecretCipherError):
        cipher.encrypt("synthetic secret", **CONTEXT)


@pytest.mark.parametrize("offset", [0, -1])
def test_ciphertext_or_tag_manipulation_is_rejected(cipher, offset):
    encrypted = cipher.encrypt("synthetic secret", **CONTEXT)
    modified = bytearray(encrypted.ciphertext)
    modified[offset] ^= 1
    with pytest.raises(SecretDecryptionError):
        cipher.decrypt(replace(encrypted, ciphertext=bytes(modified)), **CONTEXT)
    assert cipher.decrypt(encrypted, **CONTEXT) == "synthetic secret"


def test_nonce_manipulation_is_rejected(cipher):
    encrypted = cipher.encrypt("synthetic secret", **CONTEXT)
    modified = bytes([encrypted.nonce[0] ^ 1]) + encrypted.nonce[1:]
    with pytest.raises(SecretDecryptionError):
        cipher.decrypt(replace(encrypted, nonce=modified), **CONTEXT)


@pytest.mark.parametrize(
    "context",
    [
        {"merchant_id": OTHER_ID, "endpoint_id": ENDPOINT_ID},
        {"merchant_id": MERCHANT_ID, "endpoint_id": OTHER_ID},
        {"merchant_id": ENDPOINT_ID, "endpoint_id": MERCHANT_ID},
    ],
)
def test_secret_is_bound_to_merchant_and_endpoint(cipher, context):
    encrypted = cipher.encrypt("synthetic secret", **CONTEXT)
    with pytest.raises(SecretDecryptionError):
        cipher.decrypt(encrypted, **context)


def test_key_identifier_is_authenticated_even_when_keys_match(cipher):
    aliased = SecretCipher(
        active_key_id="key-1", keys={"key-1": KEYS["key-1"], "alias": KEYS["key-1"]}
    )
    encrypted = cipher.encrypt("synthetic secret", **CONTEXT)
    with pytest.raises(SecretDecryptionError):
        aliased.decrypt(replace(encrypted, key_id="alias"), **CONTEXT)


def test_rotation_keeps_old_records_readable_and_uses_new_active_key(cipher):
    original = cipher.encrypt("synthetic secret", **CONTEXT)
    rotated = SecretCipher(active_key_id="key-2", keys=KEYS)
    assert rotated.decrypt(original, **CONTEXT) == "synthetic secret"
    reencrypted = rotated.encrypt(rotated.decrypt(original, **CONTEXT), **CONTEXT)
    assert reencrypted.key_id == "key-2"
    new_key_only = SecretCipher(active_key_id="key-2", keys={"key-2": KEYS["key-2"]})
    assert new_key_only.decrypt(reencrypted, **CONTEXT) == "synthetic secret"
    with pytest.raises(SecretDecryptionError):
        new_key_only.decrypt(original, **CONTEXT)


def test_wrong_key_is_rejected(cipher):
    encrypted = cipher.encrypt("synthetic secret", **CONTEXT)
    wrong = SecretCipher(active_key_id="key-1", keys={"key-1": KEYS["key-2"]})
    with pytest.raises(SecretDecryptionError) as error:
        wrong.decrypt(encrypted, **CONTEXT)
    assert str(error.value) == "Unable to decrypt secret."


@pytest.mark.parametrize(
    "changes",
    [
        {"version": 2},
        {"version": True},
        {"key_id": "missing"},
        {"key_id": None},
        {"nonce": b"short"},
        {"nonce": "wrong-type"},
        {"ciphertext": b"short"},
        {"ciphertext": None},
    ],
)
def test_invalid_encrypted_metadata_is_rejected(cipher, changes):
    encrypted = cipher.encrypt("synthetic secret", **CONTEXT)
    with pytest.raises(SecretDecryptionError):
        cipher.decrypt(replace(encrypted, **changes), **CONTEXT)


def test_invalid_plaintext_or_context_is_rejected(cipher):
    with pytest.raises(SecretCipherError):
        cipher.encrypt(b"synthetic secret", **CONTEXT)
    with pytest.raises(SecretCipherError):
        cipher.encrypt("synthetic secret", merchant_id=str(MERCHANT_ID), endpoint_id=ENDPOINT_ID)
    encrypted = cipher.encrypt("synthetic secret", **CONTEXT)
    with pytest.raises(SecretDecryptionError):
        cipher.decrypt(encrypted, merchant_id=MERCHANT_ID, endpoint_id=str(ENDPOINT_ID))


def test_authenticated_plaintext_must_be_utf8(cipher):
    encrypted = cipher.encrypt("synthetic secret", **CONTEXT)
    associated_data = (
        b"PulsePay/webhook-secret/v1\0key-1\0"
        + MERCHANT_ID.bytes
        + ENDPOINT_ID.bytes
    )
    invalid_utf8 = AESGCM(KEYS["key-1"]).encrypt(encrypted.nonce, b"\xff", associated_data)
    with pytest.raises(SecretDecryptionError):
        cipher.decrypt(replace(encrypted, ciphertext=invalid_utf8), **CONTEXT)


def test_crypto_unavailability_is_reported_safely(monkeypatch):
    def unavailable(key):
        raise UnsupportedAlgorithm("synthetic-sensitive-marker")

    monkeypatch.setattr("app.core.secret_cipher.AESGCM", unavailable)
    with pytest.raises(SecretConfigurationError) as error:
        SecretCipher(active_key_id="key-1", keys=KEYS)
    assert "synthetic-sensitive-marker" not in "".join(traceback.format_exception(error.value))


@pytest.mark.parametrize("size", [0, 16, 24, 31, 33])
def test_only_256_bit_keys_are_accepted(size):
    with pytest.raises(SecretConfigurationError):
        SecretCipher(active_key_id="key-1", keys={"key-1": bytes(size)})


@pytest.mark.parametrize(
    "active_key_id, keys",
    [
        ("key-1", {}),
        ("key-1", None),
        ("missing", KEYS),
        (None, KEYS),
        ("key-1", {"key-1": "synthetic-sensitive-marker"}),
        ("invalid/id", {"invalid/id": KEYS["key-1"]}),
    ],
)
def test_missing_or_invalid_key_configuration_is_rejected(active_key_id, keys):
    with pytest.raises(SecretConfigurationError) as error:
        SecretCipher(active_key_id=active_key_id, keys=keys)
    assert "synthetic-sensitive-marker" not in str(error.value)


def test_key_mapping_is_copied_at_construction():
    keys = {"key-1": KEYS["key-1"]}
    cipher = SecretCipher(active_key_id="key-1", keys=keys)
    encrypted = cipher.encrypt("synthetic secret", **CONTEXT)
    keys["key-1"] = KEYS["key-2"]
    assert cipher.decrypt(encrypted, **CONTEXT) == "synthetic secret"


def test_settings_load_external_key_file_path(tmp_path, monkeypatch):
    path = tmp_path / "synthetic-keys.json"
    write_key_file(path)
    monkeypatch.setenv("WEBHOOK_KEYRING_FILE", str(path))
    settings = Settings(_env_file=None)
    cipher = SecretCipher.from_key_file(settings.webhook_keyring_file)
    assert cipher.decrypt(cipher.encrypt("synthetic secret", **CONTEXT), **CONTEXT) == "synthetic secret"


def test_missing_key_file_has_no_fallback():
    settings = Settings(_env_file=None, webhook_keyring_file=None)
    with pytest.raises(SecretConfigurationError):
        SecretCipher.from_key_file(settings.webhook_keyring_file)


def test_unavailable_key_file_is_reported_safely(tmp_path):
    path = tmp_path / "synthetic-sensitive-marker"
    with pytest.raises(SecretConfigurationError) as error:
        SecretCipher.from_key_file(path)
    assert "synthetic-sensitive-marker" not in "".join(traceback.format_exception(error.value))


@pytest.mark.parametrize(
    "content",
    [
        "synthetic-sensitive-marker",
        "[]",
        "{}",
        '{"active_key_id": "key-1", "keys": []}',
        '{"active_key_id": "key-1", "keys": {"key-1": "synthetic-sensitive-marker"}}',
        '{"active_key_id": "key-1", "keys": {"key-1": 123}}',
        '{"active_key_id": "key-1", "keys": {}, "keys": {}}',
    ],
)
def test_invalid_key_file_is_rejected_without_exposing_content(tmp_path, content):
    path = tmp_path / "synthetic-keys.json"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(SecretConfigurationError) as error:
        SecretCipher.from_key_file(path)
    assert "synthetic-sensitive-marker" not in "".join(traceback.format_exception(error.value))


def test_duplicate_key_identifiers_are_rejected(tmp_path):
    path = tmp_path / "synthetic-keys.json"
    encoded_key = base64.b64encode(KEYS["key-1"]).decode("ascii")
    path.write_text(
        '{"active_key_id":"key-1","keys":{"key-1":"'
        + encoded_key
        + '","key-1":"'
        + encoded_key
        + '"}}',
        encoding="utf-8",
    )
    with pytest.raises(SecretConfigurationError):
        SecretCipher.from_key_file(path)


@pytest.mark.parametrize("size", [16, 24, 33])
def test_key_file_enforces_aes256_key_size(tmp_path, size):
    path = tmp_path / "synthetic-keys.json"
    write_key_file(path, keys={"key-1": bytes(size)})
    with pytest.raises(SecretConfigurationError):
        SecretCipher.from_key_file(path)


def test_unreadable_key_file_is_reported_safely(tmp_path, monkeypatch):
    path = tmp_path / "synthetic-keys.json"
    write_key_file(path)

    def denied(*args, **kwargs):
        raise PermissionError("synthetic-sensitive-marker")

    monkeypatch.setattr("app.core.secret_cipher.Path.read_text", denied)
    with pytest.raises(SecretConfigurationError) as error:
        SecretCipher.from_key_file(path)
    assert "synthetic-sensitive-marker" not in "".join(traceback.format_exception(error.value))


def test_key_file_can_be_recovered_and_rotated(tmp_path):
    path = tmp_path / "synthetic-keys.json"
    path.write_text("invalid", encoding="utf-8")
    with pytest.raises(SecretConfigurationError):
        SecretCipher.from_key_file(path)
    write_key_file(path)
    cipher = SecretCipher.from_key_file(path)
    encrypted = cipher.encrypt("synthetic secret", **CONTEXT)
    write_key_file(path, active_key_id="key-2")
    rotated = SecretCipher.from_key_file(path)
    assert rotated.decrypt(encrypted, **CONTEXT) == "synthetic secret"
    assert rotated.encrypt("synthetic secret", **CONTEXT).key_id == "key-2"
