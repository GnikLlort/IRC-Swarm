"""Core data models, enums and immutable value objects.

Everything shared across the bot manager lives here so the rest of the
package only ever deals with a small, well typed vocabulary of objects.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class ProxyScheme(str, Enum):
    """Supported upstream proxy protocols."""

    SOCKS5 = "socks5"
    HTTP = "http"


class ProxyMode(str, Enum):
    """How proxies are distributed to bots."""

    UNIQUE = "unique"
    ROTATE = "rotate"
    DIRECT = "direct"


class ProxyStatus(str, Enum):
    AVAILABLE = "AVAILABLE"
    FAILED = "FAILED"
    DISABLED = "DISABLED"
    ASSIGNED = "ASSIGNED"


class BotStatus(str, Enum):
    OFFLINE = "OFFLINE"
    CONNECTING = "CONNECTING"
    CONNECTED = "CONNECTED"
    RECONNECTING = "RECONNECTING"
    QUITTING = "QUITTING"
    STOPPED = "STOPPED"


@dataclass
class Proxy:
    """A validated proxy entry parsed from ``proxy.txt``.

    Passwords are held only in this private field and are never rendered by
    ``display``/``__str__``/``as_dict`` so they can never leak into logs.
    """

    index: int                       # 1 based human facing index
    scheme: ProxyScheme
    host: str
    port: int
    username: Optional[str] = None
    _password: Optional[str] = field(default=None, repr=False)
    status: ProxyStatus = ProxyStatus.AVAILABLE
    failures: int = 0
    assigned_to: Optional[str] = None   # bot id currently using it
    last_error: str = ""
    _source_line: Optional[int] = None

    @property
    def password(self) -> Optional[str]:
        return self._password

    @property
    def label(self) -> str:
        """Human facing short label, e.g. ``socks5 #01``."""
        return f"{self.scheme.value} #{self.index:02d}"

    @property
    def endpoint(self) -> str:
        return f"{self.host}:{self.port}"

    def display(self) -> str:
        """Password-free representation used for status output and logging."""
        return f"{self.scheme.value}://{self.endpoint}"

    def as_dict(self, safe: bool = True) -> dict:
        data = {
            "index": self.index,
            "scheme": self.scheme.value,
            "host": self.host,
            "port": self.port,
            "username": self.username,
            "status": self.status.value,
            "failures": self.failures,
            "assigned_to": self.assigned_to,
        }
        if not safe:
            data["password"] = self._password
        return data

    def __str__(self) -> str:  # never reveal credentials
        return self.display()

    def __repr__(self) -> str:  # pragma: no cover - debug only, still safe
        return (
            f"Proxy(index={self.index}, scheme={self.scheme.value!r}, "
            f"host={self.host!r}, port={self.port}, status={self.status.value})"
        )


@dataclass
class IrcMessage:
    """A parsed, validated IRC protocol line."""

    raw: str
    prefix: str = ""
    command: str = ""
    params: list = field(default_factory=list)
    has_trailing: bool = False

    # -- convenience accessors -------------------------------------------
    @property
    def sender_nick(self) -> str:
        """Extract the nick portion from the prefix (``nick!user@host``)."""
        if not self.prefix:
            return ""
        if "!" in self.prefix:
            return self.prefix.split("!", 1)[0]
        if "@" in self.prefix:
            return self.prefix.split("@", 1)[0]
        return self.prefix

    @property
    def text(self) -> str:
        """Return the trailing text param (the message payload), if any."""
        if self.has_trailing and self.params:
            return self.params[-1]
        return ""

    def param(self, idx: int, default: str = "") -> str:
        if 0 <= idx < len(self.params):
            return self.params[idx]
        return default


@dataclass
class BotStats:
    """Mutable counters maintained by a single bot."""

    messages_sent: int = 0
    messages_received: int = 0
    reconnects: int = 0
    started_at: float = field(default_factory=time.monotonic)

    @property
    def uptime(self) -> float:
        return time.monotonic() - self.started_at


@dataclass
class CommandResult:
    """Outcome of a single bot participating in a (possibly global) command."""

    bot_id: str
    ok: bool
    message: str = ""


class CommandError(Exception):
    """Raised for bad user input to the CLI / command layer."""


class ConfigError(Exception):
    """Raised when the JSON configuration is invalid."""
