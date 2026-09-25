from __future__ import annotations

import pytest

from sqltransfer_app.models import DBProfile, SSHProfile
from sqltransfer_app.profiles import (
    badges,
    destination_warning,
    endpoint_label,
    filter_db_profiles,
    filter_ssh_profiles,
    is_local,
)


def _db(**overrides) -> DBProfile:
    values = dict(
        id=1,
        name="local-mysql",
        db_type="mysql",
        host="127.0.0.1",
        port=3306,
        database="shop",
        username="root",
    )
    values.update(overrides)
    return DBProfile(**values)


def _ssh(**overrides) -> SSHProfile:
    values = dict(id=7, name="jump", host="jump.example.com", port=22, username="deploy", private_key_path="~/.ssh/id")
    values.update(overrides)
    return SSHProfile(**values)


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "LocalHost", "::1", "[::1]", "127.1.2.3"])
def test_loopback_without_a_tunnel_is_local(host):
    assert is_local(_db(host=host)) is True


@pytest.mark.parametrize("host", ["db.example.com", "10.0.0.5", "192.168.1.20", ""])
def test_any_other_host_is_not_local(host):
    assert is_local(_db(host=host)) is False


def test_a_tunnel_makes_even_localhost_remote():
    # Through SSH, 127.0.0.1 is the far end of the tunnel: somebody else's machine.
    assert is_local(_db(host="127.0.0.1", use_ssh=True, ssh_profile_id=7)) is False


def test_local_destinations_need_no_warning():
    assert destination_warning(_db()) is None


def test_remote_host_warns_with_the_target_spelled_out():
    warning = destination_warning(_db(name="prod", host="db.example.com", port=3307, database="shop"))
    assert warning is not None
    assert warning.target == "db.example.com:3307/shop"
    assert "db.example.com" in warning.headline
    assert warning.reason == "a remote host"
    assert warning.tunnel is None


def test_tunnelled_destination_warns_and_names_the_tunnel():
    warning = destination_warning(_db(use_ssh=True, ssh_profile_id=7), _ssh())
    assert warning is not None
    assert warning.reason == "an SSH tunnel"
    assert warning.tunnel == "jump (deploy@jump.example.com:22)"


def test_tunnelled_destination_warns_even_when_the_ssh_profile_is_unknown():
    warning = destination_warning(_db(use_ssh=True, ssh_profile_id=7), None)
    assert warning is not None and warning.tunnel is None


def test_endpoint_label_reads_like_a_connection_string():
    assert endpoint_label(_db(username="reader", host="db.example.com", port=5432, database="shop", db_type="postgres")) == (
        "postgres://reader@db.example.com:5432/shop"
    )


def test_badges_show_what_deviates_from_plain_and_local():
    assert badges(_db()) == ("local",)
    assert badges(_db(host="db.example.com")) == ("remote",)
    assert badges(_db(use_ssh=True, ssh_profile_id=7)) == ("remote", "SSH")
    assert badges(_db(host="db.example.com", tls_mode="verified")) == ("remote", "TLS verified")
    assert badges(_db(tls_mode="off")) == ("local", "TLS off")


@pytest.mark.parametrize(
    "query,expected",
    [
        ("", ["local-mysql", "prod-shop", "stage"]),
        ("  ", ["local-mysql", "prod-shop", "stage"]),
        ("prod", ["prod-shop"]),
        ("PROD", ["prod-shop"]),  # case does not matter
        ("example.com", ["prod-shop", "stage"]),  # host
        ("orders", ["stage"]),  # database name
        ("reader", ["stage"]),  # username
        ("mysql", ["local-mysql", "prod-shop"]),  # type, and the name of the first
        ("nothing-here", []),
    ],
)
def test_filter_matches_name_host_database_user_and_type(query, expected):
    profiles = [
        _db(id=1, name="local-mysql"),
        _db(id=2, name="prod-shop", host="db.example.com", database="shop"),
        _db(id=3, name="stage", db_type="postgres", host="stage.example.com", database="orders", username="reader"),
    ]
    assert [p.name for p in filter_db_profiles(profiles, query)] == expected


def test_ssh_filter_matches_name_host_and_user():
    profiles = [_ssh(id=1, name="jump"), _ssh(id=2, name="other", host="two.example.com", username="ops")]
    assert [p.name for p in filter_ssh_profiles(profiles, "ops")] == ["other"]
    assert [p.name for p in filter_ssh_profiles(profiles, "jump.example.com")] == ["jump"]
    assert len(filter_ssh_profiles(profiles, "")) == 2
