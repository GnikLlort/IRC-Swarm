"""JSON configuration loading, defaults and validation.

Configuration is intentionally validated hard at startup (and on
``reload-config``) so that a typo never causes a confusing runtime failure
later on.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List

from models import ConfigError, ProxyMode


# --------------------------------------------------------------------------- #
# Defaults
# --------------------------------------------------------------------------- #
DEFAULT_BOT_COUNT = 1
DEFAULT_NICK_PREFIX = ""


@dataclass
class RateLimitConfig:
    messages_per_second: float = 2.0
    burst: float = 3.0

    @classmethod
    def from_dict(cls, d: Any) -> "RateLimitConfig":
        if d in (None, {}):
            return cls()
        if not isinstance(d, dict):
            raise ConfigError("'rate_limit' must be an object")
        mps = d.get("messages_per_second", cls.messages_per_second)
        burst = d.get("burst", cls.burst)
        return cls(messages_per_second=_positive(mps, "rate_limit.messages_per_second"),
                   burst=_positive(burst, "rate_limit.burst"))


@dataclass
class ReconnectConfig:
    initial_delay: float = 2.0
    max_delay: float = 60.0
    factor: float = 2.0

    @classmethod
    def from_dict(cls, d: Any) -> "ReconnectConfig":
        if d in (None, {}):
            return cls()
        if not isinstance(d, dict):
            raise ConfigError("'reconnect' must be an object")
        initial = d.get("initial_delay", cls.initial_delay)
        maxd = d.get("max_delay", cls.max_delay)
        factor = d.get("factor", cls.factor)
        if _positive(initial, "reconnect.initial_delay") >= _positive(maxd, "reconnect.max_delay"):
            raise ConfigError("'reconnect.initial_delay' must be < 'reconnect.max_delay'")
        return cls(initial_delay=float(initial), max_delay=float(maxd),
                   factor=_positive(factor, "reconnect.factor"))


@dataclass
class IRCSettings:
    server: str
    port: int = 6697
    tls: bool = True
    channels: List[str] = field(default_factory=list)
    nickname_prefix: str = DEFAULT_NICK_PREFIX
    username: str = "admin"
    realname: str = "IRC Bot Manager"
    password: str | None = None
    verify_tls: bool = True
    sasl: bool = False

    @classmethod
    def from_dict(cls, d: Any) -> "IRCSettings":
        if not isinstance(d, dict):
            raise ConfigError("'irc' section must be an object")
        server = (d.get("server") or "").strip()
        if not server:
            raise ConfigError("'irc.server' is required")
        port = int(d.get("port", 6697))
        if not (1 <= port <= 65535):
            raise ConfigError(f"invalid 'irc.port': {port}")
        channels = d.get("channels", [])
        if isinstance(channels, str):
            channels = [channels]
        if not isinstance(channels, list) or not all(isinstance(c, str) for c in channels):
            raise ConfigError("'irc.channels' must be a list of strings")
        channels = [c if c.startswith(("#", "&", "+", "!")) else f"#{c}" for c in channels]
        return cls(
            server=server,
            port=port,
            tls=bool(d.get("tls", True)),
            channels=list(dict.fromkeys(channels)),
            nickname_prefix=(d.get("nickname_prefix") or "").strip(),
            username=d.get("username", "admin") or "admin",
            realname=d.get("realname", "IRC Bot Manager") or "IRC Bot Manager",
            password=d.get("password") or None,
            verify_tls=bool(d.get("verify_tls", True)),
            sasl=bool(d.get("sasl", False)),
        )


@dataclass
class AppConfig:
    bot_count: int
    nickname_prefix: str = DEFAULT_NICK_PREFIX
    irc: IRCSettings = field(default_factory=lambda: IRCSettings("localhost"))
    proxy_mode: ProxyMode = ProxyMode.ROTATE
    proxy_file: str = "proxy.txt"
    proxy_health_check: bool = False
    proxy_timeout: float = 5.0
    connect_timeout: float = 10.0
    reassign_failed_proxy: bool = True
    fallback_direct_on_proxy_failure: bool = False
    direct_when_no_proxy: bool = False
    rate_limit: RateLimitConfig = field(default_factory=RateLimitConfig)
    reconnect: ReconnectConfig = field(default_factory=ReconnectConfig)
    groups: Dict[str, List[str]] = field(default_factory=dict)
    max_line_length: int = 512
    auto_connect: bool = True
    log_level: str = "INFO"
    log_dir: str = "logs"
    start_stopped: bool = False          # used by tests to keep bots from auto-connecting

    # ------------------------------------------------------------------ #
    def bot_ids(self) -> List[str]:
        return [f"bot{i:0{self.id_width}d}" for i in range(1, self.bot_count + 1)]

    @property
    def id_width(self) -> int:
        return max(3, len(str(max(self.bot_count, 1))))

    def nick_for(self, bot_id: str) -> str:
        idx = int(bot_id[3:])
        prefix = self.nickname_prefix or "Bot"
        return f"{prefix}{idx:0{self.id_width}d}"

    def display_groups(self) -> Dict[str, List[str]]:
        known = set(self.bot_ids())
        out: Dict[str, List[str]] = {}
        for name, members in self.groups.items():
            out[name] = [b for b in members if b in known]
        return out


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #
def _positive(value: Any, key: str) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"'{key}' must be a number") from exc
    if v <= 0:
        raise ConfigError(f"'{key}' must be > 0")
    return v


def load_config(source: str | dict) -> AppConfig:
    """Build a validated :class:`AppConfig` from a JSON string/dict/file path."""
    if isinstance(source, str):
        if os.path.isfile(source):
            with open(source, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
        else:
            raw = json.loads(source)
    elif isinstance(source, dict):
        raw = source
    else:
        raise ConfigError("config source must be a path, JSON string or dict")

    if not isinstance(raw, dict):
        raise ConfigError("configuration root must be an object")

    bot_count = int(raw.get("bot_count", DEFAULT_BOT_COUNT))
    if bot_count < 1 or bot_count > 10000:
        raise ConfigError(f"'bot_count' must be between 1 and 10000, got {bot_count}")

    irc = IRCSettings.from_dict(raw.get("irc", {}))

    # effective nickname prefix: irc.nickname_prefix takes priority if set,
    # otherwise fall back to a top-level nickname_prefix.
    prefix = raw.get("nickname_prefix") or irc.nickname_prefix or ""
    irc.nickname_prefix = prefix

    pm = raw.get("proxy_mode", "rotate")
    if pm is None:
        pm = "rotate"
    if not isinstance(pm, str) or pm not in {"unique", "rotate", "direct"}:
        raise ConfigError("'proxy_mode' must be one of: unique, rotate, direct")
    proxy_mode = ProxyMode(pm)

    groups = raw.get("groups") or {}
    if not isinstance(groups, dict):
        raise ConfigError("'groups' must be an object mapping name -> bot list")
    norm_groups: Dict[str, List[str]] = {}
    for name, members in groups.items():
        if not isinstance(members, list) or not all(isinstance(m, str) for m in members):
            raise ConfigError(f"group {name!r} must be a list of bot ids")
        norm_groups[name] = list(members)

    # validate group memberships reference real bots
    if norm_groups:
        real = set()
        for members in norm_groups.values():
            real |= set(members)
        if real and bot_count:
            width = max(3, len(str(max(bot_count, 1))))
            known = {f"bot{i:0{width}d}" for i in range(1, bot_count + 1)}
            unknown = real - known
            if unknown:
                raise ConfigError(f"group references unknown bot ids: {sorted(unknown)}")

    return AppConfig(
        bot_count=bot_count,
        nickname_prefix=prefix,
        irc=irc,
        proxy_mode=proxy_mode,
        proxy_file=raw.get("proxy_file", "proxy.txt"),
        proxy_health_check=bool(raw.get("proxy_health_check", False)),
        proxy_timeout=_positive(raw.get("proxy_timeout", 5.0), "proxy_timeout"),
        connect_timeout=_positive(raw.get("connect_timeout", 10.0), "connect_timeout"),
        reassign_failed_proxy=bool(raw.get("reassign_failed_proxy", True)),
        fallback_direct_on_proxy_failure=bool(raw.get("fallback_direct_on_proxy_failure", False)),
        direct_when_no_proxy=bool(raw.get("direct_when_no_proxy", False)),
        rate_limit=RateLimitConfig.from_dict(raw.get("rate_limit", {})),
        reconnect=ReconnectConfig.from_dict(raw.get("reconnect", {})),
        groups=norm_groups,
        max_line_length=int(raw.get("max_line_length", 512)),
        auto_connect=bool(raw.get("auto_connect", True)),
        log_level=str(raw.get("log_level", "INFO")).upper(),
        log_dir=str(raw.get("log_dir", "logs")),
        start_stopped=bool(raw.get("start_stopped", False)),
    )


def load_config_file(path: str) -> AppConfig:
    return load_config(path)


def _dump(cfg: AppConfig) -> dict:
    """Serialise a config back to a plain dict (handy for tests)."""
    return {
        "bot_count": cfg.bot_count,
        "nickname_prefix": cfg.nickname_prefix,
        "irc": {
            "server": cfg.irc.server,
            "port": cfg.irc.port,
            "tls": cfg.irc.tls,
            "channels": cfg.irc.channels,
            "username": cfg.irc.username,
            "realname": cfg.irc.realname,
        },
        "proxy_mode": cfg.proxy_mode.value,
        "proxy_file": cfg.proxy_file,
        "proxy_health_check": cfg.proxy_health_check,
        "reassign_failed_proxy": cfg.reassign_failed_proxy,
        "rate_limit": {"messages_per_second": cfg.rate_limit.messages_per_second,
                       "burst": cfg.rate_limit.burst},
        "reconnect": {"initial_delay": cfg.reconnect.initial_delay,
                      "max_delay": cfg.reconnect.max_delay,
                      "factor": cfg.reconnect.factor},
        "groups": cfg.groups,
    }
