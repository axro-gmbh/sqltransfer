# tests/test_secrets_salt.py
from __future__ import annotations

from sqltransfer_app.secrets import SALT_KEY, SecretStore


class _Memory(SecretStore):
    """The real class with the Keychain swapped for a dict."""

    def __init__(self):
        super().__init__()
        self.values: dict[str, str] = {}
        self.writes = 0

    def set_secret(self, key, value):
        self.writes += 1
        self.values[key] = value

    def get_secret(self, key):
        return self.values.get(key)


def test_the_salt_is_created_once_and_reused():
    store = _Memory()
    first = store.anonymization_salt()
    assert first == store.anonymization_salt()
    assert store.writes == 1
    assert store.values[SALT_KEY] == first


def test_the_salt_is_long_random_hex():
    store = _Memory()
    salt = store.anonymization_salt()
    assert len(salt) == 32 and int(salt, 16) >= 0
    assert salt != _Memory().anonymization_salt()
