from __future__ import annotations

try:
    import keyring
    from keyring.errors import PasswordDeleteError
except ImportError:  # pragma: no cover
    keyring = None

    class PasswordDeleteError(Exception):
        pass


class SecretStore:
    def __init__(self, service_name: str = "sqltransfer") -> None:
        self.service_name = service_name

    def set_secret(self, key: str, value: str) -> None:
        if keyring is None:
            raise RuntimeError("keyring dependency is not installed")
        keyring.set_password(self.service_name, key, value)

    def get_secret(self, key: str | None) -> str | None:
        if not key:
            return None
        if keyring is None:
            return None
        return keyring.get_password(self.service_name, key)

    def delete_secret(self, key: str | None) -> None:
        if not key:
            return
        if keyring is None:
            return
        try:
            keyring.delete_password(self.service_name, key)
        except PasswordDeleteError:
            return


