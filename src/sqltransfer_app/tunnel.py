from __future__ import annotations

from dataclasses import dataclass
import socket
import subprocess
import time
from typing import Any, Optional

try:
    from sshtunnel import SSHTunnelForwarder
    _HAS_SSHTUNNEL = True
except ImportError:  # pragma: no cover
    SSHTunnelForwarder = Any
    _HAS_SSHTUNNEL = False

from .models import SSHProfile


@dataclass(slots=True)
class OpenTunnel:
    backend: str
    forwarder: SSHTunnelForwarder | None
    process: subprocess.Popen[str] | None
    local_host: str
    local_port: int


class TunnelManager:
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
            "-L",
            f"127.0.0.1:{local_port}:{remote_host}:{remote_port}",
            f"{ssh_profile.username}@{ssh_profile.host}",
        ]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        time.sleep(0.2)
        if proc.poll() is not None:
            stderr = (proc.stderr.read() if proc.stderr else "").strip()
            raise RuntimeError(stderr or "OpenSSH tunnel process exited early")
        try:
            self._wait_local_port("127.0.0.1", local_port)
        except Exception:
            proc.terminate()
            raise
        return OpenTunnel(
            backend="ssh-cli",
            forwarder=None,
            process=proc,
            local_host="127.0.0.1",
            local_port=local_port,
        )

    def _open_with_sshtunnel(
        self,
        ssh_profile: SSHProfile,
        remote_host: str,
        remote_port: int,
        passphrase: Optional[str],
    ) -> OpenTunnel:
        if not _HAS_SSHTUNNEL:
            raise RuntimeError("sshtunnel dependency is not installed")
        try:
            forwarder = SSHTunnelForwarder(
                ssh_address_or_host=(ssh_profile.host, ssh_profile.port),
                ssh_username=ssh_profile.username,
                ssh_pkey=ssh_profile.private_key_path,
                ssh_private_key_password=passphrase,
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
        forwarder.start()
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
            except Exception:
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





