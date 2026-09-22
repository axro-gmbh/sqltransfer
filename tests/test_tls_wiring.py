from __future__ import annotations

from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import sqltransfer_app
from sqltransfer_app.models import DBProfile
from sqltransfer_app.tls import TlsSettings
from sqltransfer_app.transfer import build_dsn


def test_every_database_connection_goes_through_the_tls_helpers():
    # One direct pymysql/psycopg call would silently ignore the encryption setting.
    package = Path(sqltransfer_app.__file__).parent
    direct = [
        f"{path.name}: {line.strip()}"
        for path in sorted(package.glob("*.py"))
        for line in path.read_text(encoding="utf-8").splitlines()
        if "pymysql.connect(" in line or "psycopg.connect(" in line
    ]
    assert direct == [
        "transfer.py: return pymysql.connect(**kwargs, **mysql_connect_kwargs(tls))",
        "transfer.py: return psycopg.connect(**kwargs, **postgres_connect_kwargs(tls))",
    ]


def _profile(db_type: str) -> DBProfile:
    return DBProfile(id=None, name="p", role="remote", db_type=db_type, host="db.example.com",
                     port=3306, database="shop", username="reader")


def _query(dsn: str) -> dict[str, list[str]]:
    return parse_qs(urlsplit(dsn).query)


def test_mysql_dsn_carries_the_ssl_mode_explicitly():
    dsn = build_dsn(_profile("mysql"), "pw", tls=TlsSettings("required"))
    assert dsn.startswith("mysql://reader:pw@db.example.com:3306/shop?")
    assert _query(dsn) == {"ssl-mode": ["required"]}


def test_mysql_dsn_through_a_tunnel_disables_tls_explicitly():
    # Leaving it out would be fine for 127.0.0.1 today, but explicit cannot drift.
    dsn = build_dsn(_profile("mysql"), "pw", host="127.0.0.1", port=51000, tls=TlsSettings("off"))
    assert _query(dsn) == {"ssl-mode": ["disabled"]}


def test_postgres_dsn_carries_sslmode_and_ca():
    dsn = build_dsn(_profile("postgres"), "pw", tls=TlsSettings("verified", "/etc/ca.pem"))
    assert _query(dsn) == {"sslmode": ["verify-full"], "sslrootcert": ["/etc/ca.pem"]}


def test_password_with_special_characters_stays_intact_next_to_the_query():
    dsn = build_dsn(_profile("mysql"), "p@ss?w&rd", tls=TlsSettings("off"))
    parts = urlsplit(dsn)
    assert parts.password == "p%40ss%3Fw%26rd"
    assert _query(dsn) == {"ssl-mode": ["disabled"]}
