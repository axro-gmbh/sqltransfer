from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import socket
import subprocess
import time
from typing import Any, Optional

import paramiko

try:
    from sshtunnel import SSHTunnelForwarder
    _HAS_SSHTUNNEL = True
except ImportError:  # pragma: no cover
    SSHTunnelForwarder = Any
    _HAS_SSHTUNNEL = False

from .models import SSHProfile

DEFAULT_KNOWN_HOSTS = Path.home() / ".ssh" / "known_hosts"


class HostKeyError(RuntimeError):
    """The SSH server could not be confirmed to be the one seen before.

    Never answered by retrying over the other backend: a changed host key is the
    one failure that must stop the connection, because it can mean interception.
    """


_HOST_KEY_FAILURE_MARKERS = (
    "host key verification failed",
    "remote host identification has changed",
    "has changed and you have requested strict checking",
)

# Same order OpenSSH prefers; used when known_hosts holds several keys for one host.
_KEY_TYPE_PREFERENCE = (
    "ssh-ed25519",
    "ecdsa-sha2-nistp256",
    "ecdsa-sha2-nistp384",
    "ecdsa-sha2-nistp521",
    "rsa-sha2-512",
    "rsa-sha2-256",
    "ssh-rsa",
)


def is_host_key_failure(stderr: str) -> bool:
    text = (stderr or "").lower()
    return any(marker in text for marker in _HOST_KEY_FAILURE_MARKERS)


def _known_hosts_name(host: str, port: int) -> str:
    return host if port == 22 else f"[{host}]:{port}"


def lookup_known_host_key(known_hosts: Path, host: str, port: int) -> paramiko.PKey | None:
    """The key stored for host:port in an OpenSSH known_hosts file, hashed entries included."""
    if not known_hosts.exists():
        return None
    keys = paramiko.HostKeys()
    keys.load(str(known_hosts))
    entry = keys.lookup(_known_hosts_name(host, port))
    if not entry:
        return None
    for key_type in _KEY_TYPE_PREFERENCE:
        if key_type in entry:
            return entry[key_type]
    return next(iter(entry.values()), None)


def remember_host_key(known_hosts: Path, host: str, port: int, key: paramiko.PKey) -> None:
    """Append a key in OpenSSH format, so the ssh command and this app share one trust store."""
    known_hosts.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with known_hosts.open("a", encoding="utf-8") as fh:
        fh.write(f"{_known_hosts_name(host, port)} {key.get_name()} {key.get_base64()}\n")


def _host_key_types(key: paramiko.PKey | None) -> tuple[str, ...] | None:
    if key is None:
        return None
    if isinstance(key, paramiko.RSAKey):
        # Same RSA key, signed with SHA-2 on modern servers.
        return ("rsa-sha2-512", "rsa-sha2-256", "ssh-rsa")
    return (key.get_name(),)


def fetch_server_host_key(
    host: str, port: int, like: paramiko.PKey | None = None, timeout_s: float = 5.0
) -> paramiko.PKey:
    """Ask the server for its host key without authenticating.

    `like` restricts negotiation to that key's type, so a server offering several
    keys is compared on the one that was stored, not on whichever it prefers.
    """
    sock = socket.create_connection((host, port), timeout_s)
    transport = paramiko.Transport(sock)
    try:
        key_types = _host_key_types(like)
        if key_types:
            transport.get_security_options().key_types = key_types
        transport.start_client(timeout=timeout_s)
        return transport.get_remote_server_key()
    finally:
        transport.close()


def _changed_key_message(host: str, port: int, known_hosts: Path) -> str:
    name = _known_hosts_name(host, port)
    return (
        f"The SSH host key of {name} does not match the one stored in {known_hosts}. "
        "Either the server was reinstalled, or someone is intercepting the connection. "
        f"Confirm with whoever runs the server, then remove the old entry with: ssh-keygen -R '{name}'"
    )


@dataclass(slots=True)
class OpenTunnel:
    backend: str
    forwarder: SSHTunnelForwarder | None
    process: subprocess.Popen[str] | None
    local_host: str
    local_port: int


class TunnelManager:
    def __init__(self, known_hosts: Path | None = None) -> None:
        # Both backends read and write the same OpenSSH file, so trusting a server
        # once in the terminal or once in the app counts for both.
        self.known_hosts = known_hosts or DEFAULT_KNOWN_HOSTS

    def _pick_local_port(self) -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            return int(s.getsockname()[1])

    def _wait_local_port(self, host: str, port: int, timeout_s: float = 4.0) -> None:
        deadline = time.time() + timeout_s
        last_err: Exception | None = None
        while time.time() < deadline:
            try:
                with socket.create_connection((host, port), 0.5):
                    return
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                time.sleep(0.1)
        if last_err:
            raise last_err
        raise TimeoutError("Timed out waiting for tunnel local port")

    def _open_with_ssh_cli(
        self,
        ssh_profile: SSHProfile,
        remote_host: str,
        remote_port: int,
    ) -> OpenTunnel:
        local_port = self._pick_local_port()
        cmd = [
            "ssh",
            "-N",
            "-i",
            ssh_profile.private_key_path,
            "-p",
            str(ssh_profile.port),
            "-o",
            "ExitOnForwardFailure=yes",
            "-o",
            "BatchMode=yes",
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            "StrictHostKeyChecking=accept-new",
            "-o",
            f"UserKnownHostsFile={self.known_hosts}",
            "-L",
            f"127.0.0.1:{local_port}:{remote_host}:{remote_port}",
            f"{ssh_profile.username}@{ssh_profile.host}",
        ]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        time.sleep(0.2)
        if proc.poll() is not None:
            self._raise_cli_failure(proc, ssh_profile)
        try:
            self._wait_local_port("127.0.0.1", local_port)
        except Exception:
            # Over a slow link ssh may only give up after the short wait above.
            if proc.poll() is not None:
                self._raise_cli_failure(proc, ssh_profile)
            proc.terminate()
            raise
        return OpenTunnel(
            backend="ssh-cli",
            forwarder=None,
            process=proc,
            local_host="127.0.0.1",
            local_port=local_port,
        )

    def _raise_cli_failure(self, proc: subprocess.Popen[str], ssh_profile: SSHProfile) -> None:
        stderr = (proc.stderr.read() if proc.stderr else "").strip()
        if is_host_key_failure(stderr):
            raise HostKeyError(
                _changed_key_message(ssh_profile.host, ssh_profile.port, self.known_hosts) + f"\n\nssh said: {stderr}"
            )
        raise RuntimeError(stderr or "OpenSSH tunnel process exited early")

    def _trusted_host_key(self, ssh_profile: SSHProfile) -> paramiko.PKey:
        """The key to demand from the server, recording it on first contact.

        Mirrors OpenSSH's StrictHostKeyChecking=accept-new, which the CLI backend
        uses: the first key seen is stored, any later change is refused.
        """
        known = lookup_known_host_key(self.known_hosts, ssh_profile.host, ssh_profile.port)
        if known is not None:
            return known
        key = fetch_server_host_key(ssh_profile.host, ssh_profile.port)
        remember_host_key(self.known_hosts, ssh_profile.host, ssh_profile.port, key)
        return key

    def _open_with_sshtunnel(
        self,
        ssh_profile: SSHProfile,
        remote_host: str,
        remote_port: int,
        passphrase: Optional[str],
    ) -> OpenTunnel:
        if not _HAS_SSHTUNNEL:
            raise RuntimeError("sshtunnel dependency is not installed")
        host_key = self._trusted_host_key(ssh_profile)
        try:
            forwarder = SSHTunnelForwarder(
                ssh_address_or_host=(ssh_profile.host, ssh_profile.port),
                ssh_username=ssh_profile.username,
                ssh_pkey=ssh_profile.private_key_path,
                ssh_private_key_password=passphrase,
                # paramiko refuses the connection when the server presents another key.
                ssh_host_key=host_key,
                # Use explicit key-file auth only; avoids incompatible agent key discovery on newer Paramiko.
                allow_agent=False,
                host_pkey_directories=[],
                remote_bind_address=(remote_host, remote_port),
                local_bind_address=("127.0.0.1", 0),
            )
        except AttributeError as exc:
            if "DSSKey" in str(exc):
                raise RuntimeError(
                    "sshtunnel/paramiko compatibility issue detected. Pin paramiko<4 in this environment."
                ) from exc
            raise
        try:
            forwarder.start()
        except Exception:
            # sshtunnel reports every failure as "could not establish session", so
            # look again to tell a changed host key apart from a login problem.
            try:
                offered = fetch_server_host_key(ssh_profile.host, ssh_profile.port, like=host_key)
            except Exception:  # noqa: BLE001
                offered = None
            if offered is not None and offered.asbytes() != host_key.asbytes():
                raise HostKeyError(_changed_key_message(ssh_profile.host, ssh_profile.port, self.known_hosts))
            raise
        return OpenTunnel(
            backend="sshtunnel",
            forwarder=forwarder,
            process=None,
            local_host="127.0.0.1",
            local_port=int(forwarder.local_bind_port),
        )

    def open_tunnel(
        self,
        ssh_profile: SSHProfile,
        remote_host: str,
        remote_port: int,
        passphrase: Optional[str] = None,
    ) -> OpenTunnel:
        # Prefer OpenSSH for DataGrip-like behavior when no passphrase is needed.
        if passphrase is None:
            try:
                return self._open_with_ssh_cli(ssh_profile, remote_host, remote_port)
            except HostKeyError:
                # Falling back here used to route exactly this case, a changed
                # server key, onto a path that did not check host keys at all.
                raise
            except Exception:  # noqa: BLE001
                # e.g. a key that needs a passphrase fails under BatchMode; the
                # paramiko path below checks host keys against the same file.
                pass
        return self._open_with_sshtunnel(ssh_profile, remote_host, remote_port, passphrase)

    def close_tunnel(self, tunnel: OpenTunnel) -> None:
        if tunnel.forwarder is not None:
            tunnel.forwarder.stop()
            return
        if tunnel.process is not None:
            tunnel.process.terminate()
            try:
                tunnel.process.wait(timeout=2.0)
            except Exception:  # noqa: BLE001
                tunnel.process.kill()





