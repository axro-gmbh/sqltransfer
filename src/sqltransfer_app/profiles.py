"""What a connection profile means, as plain functions the UI can ask.

Three questions come up again and again: does this profile point at the machine
in front of me or at somebody else's, how is it written for a human, and does it
match what was typed into the search box. They live here so the answers are the
same everywhere and can be tested without a window.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence

from .models import DBProfile, SSHProfile
from .tls import is_loopback


@dataclass(frozen=True, slots=True)
class DestinationWarning:
    """Why writing to this destination deserves a question first."""

    host: str
    target: str  # "host:port/database"
    reason: str  # "a remote host" or "an SSH tunnel"
    tunnel: str | None = None  # the SSH profile, spelled out, when there is one

    @property
    def headline(self) -> str:
        return f"Overwrite data on {self.host}?"


def is_local(profile: DBProfile) -> bool:
    """True only for a loopback host reached directly.

    A tunnel makes even 127.0.0.1 remote: that address is then the near end of a
    forwarding to another machine, which is exactly the case this guards against.
    """
    if profile.use_ssh:
        return False
    return is_loopback(profile.host)


def endpoint_label(profile: DBProfile) -> str:
    return f"{profile.db_type}://{profile.username}@{profile.host}:{profile.port}/{profile.database}"


def ssh_label(profile: SSHProfile) -> str:
    return f"{profile.name} ({profile.username}@{profile.host}:{profile.port})"


def badges(profile: DBProfile) -> tuple[str, ...]:
    """Short markers for a list row: where it points, and what deviates from plain."""
    marks = ["local" if is_local(profile) else "remote"]
    if profile.use_ssh:
        marks.append("SSH")
    if profile.tls_mode and profile.tls_mode != "auto":
        marks.append(f"TLS {profile.tls_mode}")
    return tuple(marks)


def destination_warning(profile: DBProfile, ssh_profile: SSHProfile | None = None) -> DestinationWarning | None:
    """None when writing there is harmless, otherwise everything needed to ask."""
    if is_local(profile):
        return None
    return DestinationWarning(
        host=profile.host,
        target=f"{profile.host}:{profile.port}/{profile.database}",
        reason="an SSH tunnel" if profile.use_ssh else "a remote host",
        tunnel=ssh_label(ssh_profile) if (profile.use_ssh and ssh_profile) else None,
    )


def _matches(haystack: Iterable[object], query: str) -> bool:
    # Every word has to appear somewhere, so "prod shop" narrows instead of widening.
    text = " ".join(str(part or "") for part in haystack).lower()
    return all(word in text for word in query.lower().split())


def filter_db_profiles(profiles: Sequence[DBProfile], query: str | None) -> list[DBProfile]:
    return [
        p
        for p in profiles
        if _matches((p.name, p.db_type, p.host, p.port, p.database, p.username), query or "")
    ]


def filter_ssh_profiles(profiles: Sequence[SSHProfile], query: str | None) -> list[SSHProfile]:
    return [p for p in profiles if _matches((p.name, p.host, p.port, p.username), query or "")]
