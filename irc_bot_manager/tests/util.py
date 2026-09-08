"""Shared helpers for building test configurations and the mock server."""

from __future__ import annotations

import asyncio

import config as config_mod
from manager import BotManager


def make_cfg(mock=None, bot_count: int = 2, **overrides) -> "config_mod.AppConfig":
    """Build an AppConfig pointing at a MockServer (plaintext IRC).

    ``mock`` may be an unstarted MockServer (or None) when the caller only
    exercises proxy/assignment logic and never connects.
    """
    running = mock is not None and getattr(mock, "port", None) is not None
    host = mock.host if running else "127.0.0.1"
    port = mock.port if running else 16667
    data = {
        "bot_count": bot_count,
        "nickname_prefix": "",
        "irc": {
            "server": host,
            "port": port,
            "tls": False,
            "verify_tls": False,
            "channels": ["#test"],
            "username": "admin",
            "realname": "Test Bot",
        },
        "proxy_mode": "direct",
        "proxy_file": "unused.txt",
        "proxy_health_check": False,
        "proxy_timeout": 0.5,
        "connect_timeout": 2.0,
        "reassign_failed_proxy": True,
        "fallback_direct_on_proxy_failure": False,
        "rate_limit": {"messages_per_second": 1000, "burst": 1000},
        "reconnect": {"initial_delay": 0.05, "max_delay": 0.2, "factor": 2},
        "auto_connect": False,
        "log_level": "WARNING",
        "log_dir": "unused",
    }
    data.update(overrides)
    return config_mod.load_config(data)


async def wait_until(predicate, timeout: float = 5.0, interval: float = 0.02):
    """Poll ``predicate()`` until truthy or raise TimeoutError."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(interval)
    raise TimeoutError("condition not met within %ss" % timeout)


def count_status(manager: BotManager, status_value: str) -> int:
    return sum(1 for b in manager.bots.values() if b.state()["status"] == status_value)


def statuses(manager: BotManager) -> dict:
    out = {}
    for b in manager.bots.values():
        st = b.state()
        out[st["id"]] = st["status"]
    return out
