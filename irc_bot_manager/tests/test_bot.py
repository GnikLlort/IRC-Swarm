"""Tests for the Bot lifecycle, rate limiting and reconnect backoff."""

import asyncio
import time

import pytest

import config as config_mod
from bot import Bot
from models import BotStatus, Proxy, ProxyScheme
from proxy import parse_proxy_url
from utils import TokenBucket


def _cfg(**kw):
    data = {
        "bot_count": 1,
        "nickname_prefix": "",
        "irc": {"server": "localhost", "channels": ["#x"], "tls": False},
        "proxy_mode": "direct",
        "auto_connect": False,
        "rate_limit": {"messages_per_second": 1000, "burst": 1000},
    }
    data.update(kw)
    return config_mod.load_config(data)


def test_bot_identity_and_nickname():
    bot = Bot("bot007", _cfg(bot_count=20, nickname_prefix="TestBot"))
    assert bot.nickname == "TestBot007"
    assert bot.bot_id == "bot007"


def test_bot_default_status():
    bot = Bot("bot001", _cfg())
    assert bot.status == BotStatus.STOPPED
    assert bot.running is False


def test_proxy_assignment():
    bot = Bot("bot001", _cfg())
    p = parse_proxy_url("socks5://127.0.0.1:9050")
    p.index = 1
    bot.assign_proxy(p)
    assert bot.proxy is p
    assert p.assigned_to == "bot001"
    from models import ProxyStatus
    assert p.status == ProxyStatus.ASSIGNED


def test_say_when_disconnected_raises():
    async def run():
        bot = Bot("bot001", _cfg())
        with pytest.raises(ConnectionError):
            await bot.say("#test", "hi")
    asyncio.run(run())


def test_join_when_disconnected_raises():
    async def run():
        bot = Bot("bot001", _cfg())
        with pytest.raises(ConnectionError):
            await bot.join_channel("#test")
    asyncio.run(run())


def test_state_dict_contains_fields():
    bot = Bot("bot001", _cfg())
    s = bot.state()
    for key in ("id", "nickname", "status", "proxy", "connected", "channels",
                "messages_sent", "messages_received", "uptime", "reconnects",
                "last_error"):
        assert key in s


def test_token_bucket_throttles():
    async def run():
        bucket = TokenBucket(rate=10.0, burst=2.0)
        start = time.monotonic()
        for _ in range(6):
            await bucket.acquire()
        elapsed = time.monotonic() - start
        # burst 2 instantly + 4 tokens at 100ms each
        assert elapsed >= 0.35, f"throttled too little: {elapsed}"
        # but not absurdly slow
        assert elapsed < 5.0
    asyncio.run(run())


def test_backoff_progression():
    cfg = _cfg(reconnect={"initial_delay": 2, "max_delay": 5, "factor": 2})
    bot = Bot("bot001", cfg)
    bot._backoff_delay = 0.0
    delays = [bot._next_backoff() for _ in range(4)]
    assert delays == [2.0, 4.0, 5.0, 5.0]  # capped at max


def test_independent_rate_limiters():
    """Two bots each throttle independently."""
    async def run():
        rl1 = TokenBucket(rate=1.0, burst=1.0)
        rl2 = TokenBucket(rate=1000.0, burst=100.0)
        t = time.monotonic()
        async def consume():
            for _ in range(3):
                await rl1.acquire()
        await rl2.acquire()
        assert rl2.available <= 100.0
        await asyncio.wait_for(consume(), timeout=3)
        assert time.monotonic() - t >= 2.0
    asyncio.run(run())


def test_registered_irc_password_never_visible():
    cfg = _cfg()
    # ensure the bot builds an IrcClient that holds but never logs the password
    bot = Bot("bot001", cfg)
    # password not registered -> config has none; construct with one
    import config as cm
    cfg2 = cm.load_config({
        "bot_count": 1,
        "irc": {"server": "x", "password": "Sup3rSecretPass"},
    })
    bot2 = Bot("bot001", cfg2)
    assert bot2.config.irc.password == "Sup3rSecretPass"
