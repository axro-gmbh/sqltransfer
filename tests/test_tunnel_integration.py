"""Host key checks against a real OpenSSH server, over both tunnel backends.

Needs a disposable sshd that allows TCP forwarding and is skipped otherwise:

    SQLTRANSFER_TEST_SSH=127.0.0.1:2299:tunnel:/path/to/private_key pytest tests/test_tunnel_integration.py

Every test uses its own known_hosts file, the real ~/.ssh/known_hosts is never touched.
"""

from __future__ import annotations

import os
import socket
from pathlib import Path

import paramiko
import pytest

from sqltransfer_app.models import SSHProfile
from sqltransfer_app.tunnel import HostKeyError, TunnelManager, lookup_known_host_key, remember_host_key

_SPEC = os.environ.get("SQLTRANSFER_TEST_SSH")
pytestmark = pytest.mark.skipif(not _SPEC, reason="SQLTRANSFER_TEST_SSH not set")


def _profile() -> SSHProfile:
    host, port, user, key_path = _SPEC.split(":", 3)
    return SSHProfile(id=None, name="test", host=host, port=int(port), username=user, private_key_path=key_path)


def _banner_through(tunnel) -> bytes:
    # The tunnel points at the server's own sshd, which greets with its version string.
    with socket.create_connection((tunnel.local_host, tunnel.local_port), 5) as sock:
        return sock.recv(64)


def _plant_wrong_key(known_hosts: Path, profile: SSHProfile) -> None:
    # The server has its own ecdsa-sha2-nistp256 key; storing a different one of the
    # same type is what an impostor looks like from the client's side.
    remember_host_key(known_hosts, profile.host, profile.port, paramiko.ECDSAKey.generate())


@pytest.mark.parametrize("backend", ["_open_with_ssh_cli", "_open_with_sshtunnel"])
def test_first_contact_records_the_key_and_connects(tmp_path, backend):
    manager = TunnelManager(known_hosts=tmp_path / "known_hosts")
    profile = _profile()
    args = (profile, "127.0.0.1", 22) if backend == "_open_with_ssh_cli" else (profile, "127.0.0.1", 22, None)

    tunnel = getattr(manager, backend)(*args)
    try:
        assert _banner_through(tunnel).startswith(b"SSH-2.0")
    finally:
        manager.close_tunnel(tunnel)
    assert lookup_known_host_key(manager.known_hosts, profile.host, profile.port) is not None


def test_both_backends_accept_a_key_the_other_one_stored(tmp_path):
    manager = TunnelManager(known_hosts=tmp_path / "known_hosts")
    profile = _profile()
    first = manager._open_with_ssh_cli(profile, "127.0.0.1", 22)
    manager.close_tunnel(first)

    second = manager._open_with_sshtunnel(profile, "127.0.0.1", 22, None)
    try:
        assert _banner_through(second).startswith(b"SSH-2.0")
    finally:
        manager.close_tunnel(second)


def test_changed_key_is_refused_by_openssh_without_falling_back(tmp_path):
    manager = TunnelManager(known_hosts=tmp_path / "known_hosts")
    _plant_wrong_key(manager.known_hosts, _profile())

    with pytest.raises(HostKeyError):
        manager.open_tunnel(_profile(), "127.0.0.1", 22)


def test_changed_key_is_refused_by_the_paramiko_backend(tmp_path):
    manager = TunnelManager(known_hosts=tmp_path / "known_hosts")
    _plant_wrong_key(manager.known_hosts, _profile())

    with pytest.raises(HostKeyError):
        manager._open_with_sshtunnel(_profile(), "127.0.0.1", 22, None)
