from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(slots=True)
class SSHProfile:
    id: Optional[int]
    name: str
    host: str
    port: int
    username: str
    private_key_path: str
    passphrase_secret_key: Optional[str] = None


@dataclass(slots=True)
class DBProfile:
    id: Optional[int]
    name: str
    db_type: str  # "mysql" or "postgres"
    host: str
    port: int
    database: str
    username: str
    password_secret_key: Optional[str] = None
    use_ssh: bool = False
    ssh_profile_id: Optional[int] = None
    tls_mode: str = "auto"  # "auto" | "off" | "required" | "verified", see tls.py
    tls_ca_path: Optional[str] = None


@dataclass(slots=True)
class TransferResult:
    status: str
    rows: int
    elapsed_ms: int
    parallel: int
    message: str
