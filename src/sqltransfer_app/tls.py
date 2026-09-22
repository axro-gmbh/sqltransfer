"""Encryption settings for database connections.

One per-profile setting drives all three client libraries the app uses: apitap
(through the connection URL), pymysql and psycopg. Each has its own spelling and
its own defaults, and letting them disagree is how a password ends up encrypted
in one connection and in cleartext in the next.
"""

from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import ssl
from typing import Any

# (value, label) in the order the profile form offers them.
TLS_MODES: tuple[tuple[str, str], ...] = (
    ("auto", "Automatic"),
    ("off", "Off"),
    ("required", "Encrypted, certificate not checked"),
    ("verified", "Encrypted and verified"),
)


class TlsError(ValueError):
    """A combination of settings that cannot work."""


@dataclass(frozen=True, slots=True)
class TlsSettings:
    mode: str  # "off" | "required" | "verified", never "auto" once resolved
    ca_path: str | None = None


def is_loopback(host: str) -> bool:
    """Same rule apitap applies: localhost, 127.0.0.0/8 and ::1, written literally."""
    value = (host or "").strip().strip("[]").lower()
    if value == "localhost":
        return True
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


def resolve_tls(mode: str | None, ca_path: str | None, connect_host: str) -> TlsSettings:
    """Turn the profile setting into what applies to one concrete connection.

    `connect_host` is where the client actually connects. Through an SSH tunnel
    that is 127.0.0.1, so "auto" means no TLS inside a tunnel that already
    encrypts, and verified TLS for a direct connection to another machine. That
    is apitap's own default since 0.55.1, so both sides of the app agree.
    """
    chosen = mode if mode in {"off", "required", "verified"} else "auto"
    if chosen == "auto":
        chosen = "off" if is_loopback(connect_host) else "verified"
    return TlsSettings(chosen, ca_path or None)


def validate_tls(db_type: str, mode: str | None, ca_path: str | None) -> None:
    if not ca_path:
        return
    if db_type == "mysql":
        raise TlsError(
            "A custom CA certificate is not supported for MySQL: apitap only trusts its bundled public "
            "certificate authorities there. Use 'Encrypted, certificate not checked' for servers with an "
            "internal or self-signed certificate."
        )
    if mode in {"off", "required"}:
        raise TlsError("A CA certificate is only used when the connection is verified.")


def mysql_connect_kwargs(tls: TlsSettings) -> dict[str, Any]:
    if tls.mode == "off":
        # Without this pymysql 1.2 still tries TLS and quietly falls back to cleartext.
        return {"ssl_disabled": True}
    if tls.mode == "required":
        # A non-empty dict is what makes pymysql refuse a server that offers no TLS.
        return {"ssl": {"verify_mode": False, "check_hostname": False}}
    # pymysql switches the hostname check off when it is given no CA file, so hand
    # it a finished context instead of the ssl_verify_* flags.
    return {"ssl": _verifying_context(tls.ca_path)}


def postgres_connect_kwargs(tls: TlsSettings) -> dict[str, str]:
    if tls.mode == "off":
        return {"sslmode": "disable"}
    if tls.mode == "required":
        return {"sslmode": "require"}
    # libpq's default CA location is ~/.postgresql/root.crt; "system" (libpq 16+)
    # means the operating system's trust store, which is what people expect.
    return {"sslmode": "verify-full", "sslrootcert": tls.ca_path or "system"}


def apitap_query_params(db_type: str, tls: TlsSettings) -> dict[str, str]:
    if db_type == "mysql":
        return {"ssl-mode": {"off": "disabled", "required": "required", "verified": "verify_identity"}[tls.mode]}
    params = postgres_connect_kwargs(tls)
    if params.get("sslrootcert") == "system":
        # Leave the trust store to apitap's own default for verify-full.
        params.pop("sslrootcert")
    return params


def _verifying_context(ca_path: str | None) -> ssl.SSLContext:
    if ca_path:
        ctx = ssl.create_default_context(cafile=ca_path)
    else:
        try:
            import certifi

            ctx = ssl.create_default_context(cafile=certifi.where())
        except ImportError:  # pragma: no cover
            ctx = ssl.create_default_context()
    ctx.check_hostname = True
    ctx.verify_mode = ssl.CERT_REQUIRED
    return ctx
