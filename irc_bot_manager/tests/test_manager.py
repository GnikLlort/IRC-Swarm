"""Manager tests: fleet lifecycle, global/group commands and proxy logic.

The heavy integration scenarios run against the in-process mock IRC server.
"""

import asyncio

import pytest

from manager import BotManager
from mock_server import MockServer
from models import BotStatus
from proxy import parse_proxy_url
from util import make_cfg, wait_until, count_status, statuses


# =========================================================================== #
# Integration: full fleet against the mock server
# =========================================================================== #
def test_full_fleet_lifecycle_and_global_commands():
    async def run():
        mock = MockServer()
        await mock.start()
        manager = None
        try:
            cfg = make_cfg(mock, bot_count=3,
                           groups={"g1": ["bot001", "bot002"]})
            manager = BotManager(cfg)
            await manager.start_all()

            await wait_until(lambda: count_status(manager, "CONNECTED") == 3,
                             timeout=6)
            # every bot auto-joined #test
            for b in manager.bots.values():
                await wait_until(lambda b=b: "#test" in b.channels, timeout=3)

            # ---- say-all ----
            res = await manager.say(None, "#test", "hello everyone")
            assert sum(r.ok for r in res) == 3
            await _wait_msg(manager, mock, "bot001", "hello everyone")
            await _wait_msg(manager, mock, "bot003", "hello everyone")

            # ---- join-all into a fresh channel ----
            jres = await manager.join(None, "#report")
            assert sum(r.ok for r in jres) == 3
            for b in manager.bots.values():
                await wait_until(lambda b=b: "#report" in b.channels, timeout=3)

            # ---- group say affects only the group ----
            gres = await manager.say("g1", "#test", "group only msg")
            assert sum(r.ok for r in gres) == 2
            await _wait_msg(manager, mock, "bot001", "group only msg")
            await _wait_msg(manager, mock, "bot002", "group only msg")
            assert "group only msg" not in mock.privmsg_texts(
                manager.bots["bot003"].nickname)

            # ---- part-all from #report ----
            pres = await manager.part(None, "#report")
            assert sum(r.ok for r in pres) == 3
            for b in manager.bots.values():
                await wait_until(lambda b=b: "#report" not in b.channels,
                                 timeout=3)

            # ---- a stopped bot is reported, others still deliver ----
            await manager.stop_bot("bot003")
            res2 = await manager.say(None, "#test", "two bots only")
            byid = {r.bot_id: r for r in res2}
            assert byid["bot001"].ok and byid["bot002"].ok
            assert not byid["bot003"].ok
            assert "two bots only" not in mock.privmsg_texts(
                manager.bots["bot003"].nickname)

            # ---- reconnect a healthy bot after a server-side drop ----
            bot2 = manager.bots["bot002"]
            before = bot2.reconnect_count
            assert bot2.state()["status"] == "CONNECTED"
            await mock.drop_client(bot2.nickname)
            # allow it to notice the drop and start recovering
            await asyncio.sleep(0.05)
            await wait_until(
                lambda: bot2.state()["status"] == "CONNECTED"
                and bot2.reconnect_count >= before + 1
                and mock.client_by_nick(bot2.nickname) is not None,
                timeout=8)
            # and it is back in its channels
            await wait_until(lambda: "#test" in bot2.channels, timeout=4)

            # ---- safe command framework: !ping -> pong ----
            nick1 = manager.bots["bot001"].nickname
            await mock.say_as("tester", nick1, "!ping")
            await _wait_msg(manager, mock, "bot001", "pong")

            # ---- quit-all gracefully ----
            await manager.quit_all()
            await wait_until(lambda: count_status(manager, "CONNECTED") == 0,
                             timeout=4)
        finally:
            if manager is not None:
                await manager.shutdown()
            await mock.stop()
    asyncio.run(run())


async def _wait_msg(manager, mock, bot_id, expected):
    nick = manager.bots[bot_id].nickname
    await wait_until(lambda: expected in mock.privmsg_texts(nick), timeout=5)


# =========================================================================== #
# Integration: only say-all + status with direct proxies, but also verify a
# manager survives an individual bot proxy failure without crashing.
# =========================================================================== #
def test_individual_failure_does_not_crash_manager():
    async def run():
        mock = MockServer()
        await mock.start()
        manager = None
        try:
            cfg = make_cfg(mock, bot_count=2)
            manager = BotManager(cfg)
            await manager.start_all()
            await wait_until(lambda: count_status(manager, "CONNECTED") == 2,
                             timeout=6)
            # drop one connection hard -> its bot goes into reconnect but the
            # manager and the other bot keep running.
            await mock.drop_client(manager.bots["bot001"].nickname)
            await asyncio.sleep(0.4)
            assert manager.bots["bot002"].state()["status"] == "CONNECTED"
            res = await manager.say(None, "#test", "still alive")
            assert sum(r.ok for r in res) >= 1
        finally:
            if manager is not None:
                await manager.shutdown()
            await mock.stop()
    asyncio.run(run())


# =========================================================================== #
# Proxy assignment (no network needed)
# =========================================================================== #
def _p(index, host="203.0.113.%d" % 1):
    u = f"socks5://{host}:{1000 + index}"
    p = parse_proxy_url(u)
    p.index = index
    return p


def test_unique_assignment_not_enough_proxies():
    cfg = make_cfg(MockServer(), bot_count=5, proxy_mode="unique")
    manager = BotManager(cfg, proxies=[_p(1), _p(2), _p(3)])
    manager._assign_proxies()
    bots = list(manager.bots.values())
    assert bots[0].proxy is not None
    assert bots[1].proxy is not None
    assert bots[2].proxy is not None
    # not enough unique proxies -> bot004/bot005 stay offline (no direct)
    assert bots[3].proxy is None and bots[3].status.value == "OFFLINE"
    assert bots[4].proxy is None


def test_unique_assignment_direct_fallback():
    cfg = make_cfg(MockServer(), bot_count=5, proxy_mode="unique",
                   direct_when_no_proxy=True)
    manager = BotManager(cfg, proxies=[_p(1), _p(2), _p(3)])
    manager._assign_proxies()
    bots = list(manager.bots.values())
    assert bots[3].proxy is None
    assert bots[3].config.direct_when_no_proxy is True


def test_unique_all_proxies_distinct():
    cfg = make_cfg(MockServer(), bot_count=3, proxy_mode="unique")
    manager = BotManager(cfg, proxies=[_p(1), _p(2), _p(3)])
    manager._assign_proxies()
    proxies = [b.proxy.index for b in manager.bots.values()]
    assert sorted(proxies) == [1, 2, 3]


def test_rotate_wraps():
    cfg = make_cfg(MockServer(), bot_count=5, proxy_mode="rotate")
    manager = BotManager(cfg, proxies=[_p(1), _p(2)])
    manager._assign_proxies()
    proxies = [b.proxy.index for b in manager.bots.values()]
    assert proxies == [1, 2, 1, 2, 1]


def test_direct_mode_no_proxies():
    cfg = make_cfg(MockServer(), bot_count=3, proxy_mode="direct")
    manager = BotManager(cfg, proxies=[_p(1), _p(2), _p(3)])
    manager._assign_proxies()
    assert all(b.proxy is None for b in manager.bots.values())


def test_proxy_table_never_shows_password():
    cfg = make_cfg(MockServer(), bot_count=1)
    p = parse_proxy_url("http://user:hunter2pw@203.0.113.9:8080")
    p.index = 1
    p.assigned_to = "bot001"
    manager = BotManager(cfg, proxies=[p])
    table = manager.proxy_table()
    assert "hunter2pw" not in table
    assert "http" in table.lower()


def test_status_table_counts():
    cfg = make_cfg(MockServer(), bot_count=4)
    manager = BotManager(cfg)
    # simulate one connected, one connecting, two offline/stopped
    manager.bots["bot001"].status = BotStatus.CONNECTED
    manager.bots["bot002"].status = BotStatus.CONNECTING
    table = manager.status_table()
    assert "Connected: 1" in table
    assert "Connecting: 1" in table


# =========================================================================== #
# Proxy reassignment
# =========================================================================== #
def test_proxy_reassignment_on_failure():
    async def run():
        cfg = make_cfg(MockServer(), bot_count=1, proxy_mode="rotate")
        p_fail = parse_proxy_url("socks5://203.0.113.50:1080")
        p_fail.index = 1
        p_ok = parse_proxy_url("socks5://203.0.113.51:1080")
        p_ok.index = 2
        manager = BotManager(cfg, proxies=[p_fail, p_ok])
        bot = manager.bots["bot001"]
        bot.assign_proxy(p_fail)
        # proxy health check is off so reassignment picks the available one
        await manager.handle_proxy_connect_failure(bot)
        assert bot.proxy is p_ok
        assert p_fail.status.value == "FAILED"
        assert p_fail.assigned_to is None
    asyncio.run(run())


def test_no_replacement_and_no_direct_fallback():
    async def run():
        cfg = make_cfg(MockServer(), bot_count=1, proxy_mode="rotate",
                       fallback_direct_on_proxy_failure=False)
        p_fail = parse_proxy_url("socks5://203.0.113.60:1080")
        p_fail.index = 1
        manager = BotManager(cfg, proxies=[p_fail])
        bot = manager.bots["bot001"]
        bot.assign_proxy(p_fail)
        await manager.handle_proxy_connect_failure(bot)
        # no replacement available & direct fallback disabled -> keeps failing
        assert bot.proxy is not None
    asyncio.run(run())


# =========================================================================== #
# reload
# =========================================================================== #
def test_reload_proxies_preserves_healthy(tmp_path):
    async def run():
        proxy_file = tmp_path / "proxy.txt"
        proxy_file.write_text("socks5://203.0.113.70:1080\n"
                              "socks5://203.0.113.71:1080\n")
        cfg = make_cfg(MockServer(), bot_count=3, proxy_mode="unique")
        cfg.proxy_file = str(proxy_file)
        manager = BotManager(cfg, proxies=[])
        await manager.reload_proxies()
        assert len(manager.proxies) == 2
        # bots got proxies assigned (unique: first two)
        assert manager.bots["bot001"].proxy is not None
        assert manager.bots["bot002"].proxy is not None
        # remove the first proxy -> bot001 reassigned / bot001 loses first
        proxy_file.write_text("socks5://203.0.113.71:1080\n")
        await manager.reload_proxies()
        assert len(manager.proxies) == 1
        assert all(b.proxy is None or b.proxy.index == 1
                   for b in manager.bots.values())
    asyncio.run(run())


def test_missing_proxy_file_handled(tmp_path):
    async def run():
        cfg = make_cfg(MockServer(), bot_count=2, proxy_mode="rotate")
        cfg.proxy_file = str(tmp_path / "does-not-exist.txt")
        manager = BotManager(cfg, proxies=[])
        out = await manager.reload_proxies()
        assert manager.proxies == []
        assert "cannot reload" in out
    asyncio.run(run())
