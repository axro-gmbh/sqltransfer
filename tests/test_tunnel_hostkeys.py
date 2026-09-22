from __future__ import annotations

from pathlib import Path

import paramiko
import pytest

from sqltransfer_app.models import SSHProfile
from sqltransfer_app.tunnel import (
    HostKeyError,
    TunnelManager,
    is_host_key_failure,
    lookup_known_host_key,
    remember_host_key,
)


@pytest.fixture(scope="module")
def key_a() -> paramiko.PKey:
    return paramiko.ECDSAKey.generate()


@pytest.fixture(scope="module")
def key_b() -> paramiko.PKey:
    return paramiko.ECDSAKey.generate()


def _line(name: str, key: paramiko.PKey) -> str:
    return f"{name} {key.get_name()} {key.get_base64()}\n"


def test_missing_file_means_unknown(tmp_path: Path):
    assert lookup_known_host_key(tmp_path / "known_hosts", "jump.example", 22) is None


def test_port_22_is_stored_without_brackets(tmp_path: Path, key_a):
    kh = tmp_path / "known_hosts"
    kh.write_text(_line("jump.example", key_a))
    assert lookup_known_host_key(kh, "jump.example", 22).asbytes() == key_a.asbytes()
    assert lookup_known_host_key(kh, "jump.example", 2222) is None


def test_other_ports_use_the_openssh_bracket_form(tmp_path: Path, key_a):
    kh = tmp_path / "known_hosts"
    kh.write_text(_line("[jump.example]:2222", key_a))
    assert lookup_known_host_key(kh, "jump.example", 2222).asbytes() == key_a.asbytes()
    assert lookup_known_host_key(kh, "jump.example", 22) is None


def test_hashed_entries_are_found(tmp_path: Path, key_a):
    # OpenSSH hashes host names when HashKnownHosts is on, which is common.
    kh = tmp_path / "known_hosts"
    kh.write_text(_line(paramiko.HostKeys.hash_host("jump.example"), key_a))
    assert lookup_known_host_key(kh, "jump.example", 22).asbytes() == key_a.asbytes()


def test_remembered_key_can_be_looked_up_again(tmp_path: Path, key_a):
    kh = tmp_path / ".ssh" / "known_hosts"  # directory does not exist yet
    remember_host_key(kh, "jump.example", 2222, key_a)
    assert lookup_known_host_key(kh, "jump.example", 2222).asbytes() == key_a.asbytes()


def test_remembering_keeps_existing_entries(tmp_path: Path, key_a, key_b):
    kh = tmp_path / "known_hosts"
    kh.write_text(_line("other.example", key_b))
    remember_host_key(kh, "jump.example", 22, key_a)
    assert lookup_known_host_key(kh, "other.example", 22).asbytes() == key_b.asbytes()
    assert lookup_known_host_key(kh, "jump.example", 22).asbytes() == key_a.asbytes()


@pytest.mark.parametrize(
    "stderr",
    [
        "Host key verification failed.",
        "@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@\n"
        "@    WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED!     @\n",
        "Host key for [jump.example]:2222 has changed and you have requested strict checking.",
    ],
)
def test_openssh_host_key_messages_are_recognised(stderr):
    assert is_host_key_failure(stderr)


@pytest.mark.parametrize(
    "stderr",
    ["Permission denied (publickey).", "ssh: connect to host x port 22: Connection refused", ""],
)
def test_other_ssh_failures_are_not_host_key_failures(stderr):
    assert not is_host_key_failure(stderr)


def _profile() -> SSHProfile:
    return SSHProfile(id=None, name="t", host="jump.example", port=22, username="u", private_key_path="/k")


def test_host_key_error_is_never_answered_with_the_other_backend(monkeypatch, tmp_path):
    # The unchecked fallback used to turn exactly this case (a changed server key)
    # into a silent retry over the paramiko path.
    manager = TunnelManager(known_hosts=tmp_path / "known_hosts")
    calls: list[str] = []

    def cli(*_args, **_kwargs):
        raise HostKeyError("changed")

    def paramiko_path(*_args, **_kwargs):
        calls.append("sshtunnel")

    monkeypatch.setattr(manager, "_open_with_ssh_cli", cli)
    monkeypatch.setattr(manager, "_open_with_sshtunnel", paramiko_path)

    with pytest.raises(HostKeyError):
        manager.open_tunnel(_profile(), "127.0.0.1", 3306)
    assert calls == []


def test_other_cli_failures_still_fall_back(monkeypatch, tmp_path):
    # A key with a passphrase fails under BatchMode; that case keeps working.
    manager = TunnelManager(known_hosts=tmp_path / "known_hosts")

    def cli(*_args, **_kwargs):
        raise RuntimeError("Permission denied (publickey).")

    monkeypatch.setattr(manager, "_open_with_ssh_cli", cli)
    monkeypatch.setattr(manager, "_open_with_sshtunnel", lambda *a, **k: "sshtunnel")

    assert manager.open_tunnel(_profile(), "127.0.0.1", 3306) == "sshtunnel"
