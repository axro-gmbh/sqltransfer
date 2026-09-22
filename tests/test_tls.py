from __future__ import annotations

import ssl

import pytest

from sqltransfer_app.tls import (
    TLS_MODES,
    TlsError,
    TlsSettings,
    apitap_query_params,
    is_loopback,
    mysql_connect_kwargs,
    postgres_connect_kwargs,
    resolve_tls,
    validate_tls,
)


@pytest.mark.parametrize("host", ["localhost", "LOCALHOST", "127.0.0.1", "127.8.9.10", "::1", "[::1]"])
def test_loopback_hosts(host):
    assert is_loopback(host)


@pytest.mark.parametrize("host", ["10.139.160.200", "db.example.com", "128.0.0.1", "localhost.example.com", ""])
def test_other_hosts_are_not_loopback(host):
    assert not is_loopback(host)


def test_auto_is_off_on_this_machine_and_through_a_tunnel():
    # Through an SSH tunnel the connection goes to 127.0.0.1 and SSH already encrypts.
    assert resolve_tls("auto", None, "127.0.0.1") == TlsSettings("off")
    assert resolve_tls("auto", None, "localhost") == TlsSettings("off")


def test_auto_is_verified_everywhere_else():
    # Matches apitap since 0.55.1, which would otherwise disagree with our own connections.
    assert resolve_tls("auto", None, "10.139.160.200") == TlsSettings("verified")


def test_explicit_modes_are_kept_regardless_of_host():
    assert resolve_tls("off", None, "db.example.com") == TlsSettings("off")
    assert resolve_tls("required", None, "127.0.0.1") == TlsSettings("required")
    assert resolve_tls("verified", "/ca.pem", "127.0.0.1") == TlsSettings("verified", "/ca.pem")


def test_unknown_or_empty_mode_falls_back_to_auto():
    assert resolve_tls("", None, "db.example.com") == TlsSettings("verified")
    assert resolve_tls(None, None, "localhost") == TlsSettings("off")


def test_modes_offered_in_the_ui():
    assert [key for key, _label in TLS_MODES] == ["auto", "off", "required", "verified"]


# --- pymysql -----------------------------------------------------------------


def test_mysql_off_disables_tls_instead_of_trying_it():
    # Without anything, pymysql 1.2 tries TLS and silently falls back to cleartext.
    assert mysql_connect_kwargs(TlsSettings("off")) == {"ssl_disabled": True}


def test_mysql_required_forces_tls_without_verification():
    kwargs = mysql_connect_kwargs(TlsSettings("required"))
    # A non-empty ssl dict is what makes pymysql refuse a server without TLS.
    assert kwargs["ssl"] and kwargs["ssl"]["verify_mode"] is False and kwargs["ssl"]["check_hostname"] is False


def test_mysql_verified_checks_chain_and_hostname():
    ctx = mysql_connect_kwargs(TlsSettings("verified"))["ssl"]
    # A ready context, because pymysql turns off the hostname check when no CA file is given.
    assert isinstance(ctx, ssl.SSLContext)
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.check_hostname is True


# --- psycopg -----------------------------------------------------------------


def test_postgres_modes_map_to_libpq_sslmode():
    assert postgres_connect_kwargs(TlsSettings("off")) == {"sslmode": "disable"}
    assert postgres_connect_kwargs(TlsSettings("required")) == {"sslmode": "require"}
    # libpq's own default CA location is ~/.postgresql/root.crt; "system" means the OS store.
    assert postgres_connect_kwargs(TlsSettings("verified")) == {"sslmode": "verify-full", "sslrootcert": "system"}
    assert postgres_connect_kwargs(TlsSettings("verified", "/ca.pem")) == {
        "sslmode": "verify-full",
        "sslrootcert": "/ca.pem",
    }


# --- apitap ------------------------------------------------------------------


def test_apitap_mysql_uses_ssl_mode_spellings():
    assert apitap_query_params("mysql", TlsSettings("off")) == {"ssl-mode": "disabled"}
    assert apitap_query_params("mysql", TlsSettings("required")) == {"ssl-mode": "required"}
    assert apitap_query_params("mysql", TlsSettings("verified")) == {"ssl-mode": "verify_identity"}


def test_apitap_postgres_uses_sslmode_and_rootcert():
    assert apitap_query_params("postgres", TlsSettings("off")) == {"sslmode": "disable"}
    assert apitap_query_params("postgres", TlsSettings("verified", "/ca.pem")) == {
        "sslmode": "verify-full",
        "sslrootcert": "/ca.pem",
    }


# --- validation --------------------------------------------------------------


def test_mysql_cannot_use_a_private_ca():
    # apitap only trusts its bundled public roots for MySQL; a CA file would be silently ignored there.
    with pytest.raises(TlsError):
        validate_tls("mysql", "verified", "/ca.pem")


def test_ca_file_only_makes_sense_when_verifying():
    with pytest.raises(TlsError):
        validate_tls("postgres", "required", "/ca.pem")


def test_valid_combinations_pass():
    validate_tls("mysql", "verified", None)
    validate_tls("postgres", "verified", "/ca.pem")
    validate_tls("postgres", "auto", None)
