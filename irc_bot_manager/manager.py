"""The :class:`BotManager`.

Owns every bot, the proxy pool and the IRC server target.  Responsible for:

* proxy parsing / (optional) health checking and assignment per ``proxy_mode``
* life of the fleet (start/stop/restart on one, a group, or all)
* global ``*-all`` commands executed concurrently with per-bot error isolation
* proxy re-assignment when a proxy fails
* ``reload-proxies`` / ``reload-config`` preserving healthy assignments
* structured status/metrics for the CLI
"""

from __future__ import annotations

import asyncio
import logging
from typing import Dict, List, Optional, Tuple

from bot import Bot
from config import AppConfig
from logger import redact
from models import BotStatus, CommandResult, CommandError, ConfigError, ProxyMode, ProxyStatus
from proxy import check_proxy_connectivity, load_proxies_from_file, Proxy

log = logging.getLogger("manager")


def _key(p: Proxy) -> Tuple:
    return (p.scheme.value, p.host, p.port, p.username)


class BotManager:
    def __init__(self, config: AppConfig, proxies: Optional[List[Proxy]] = None,
                 *, logger=None) -> None:
        self.config = config
        self.logger = logger or logging.getLogger("manager")
        self.bots: Dict[str, Bot] = {}
        self.proxies: List[Proxy] = proxies or []
        self.state_callback = None   # callable(bot_id, state_dict) set by CLI
        self._build_bots()

    # ------------------------------------------------------------------ #
    # construction
    # ------------------------------------------------------------------ #
    def _build_bots(self) -> None:
        for bid in self.config.bot_ids():
            self.bots[bid] = Bot(bid, self.config, self, logger=self._bot_logger(bid))

    def _bot_logger(self, bid: str):
        from logger import facility
        return facility().bot_logger(bid)

    # ------------------------------------------------------------------ #
    # boot
    # ------------------------------------------------------------------ #
    async def startup(self) -> None:
        """Load proxies, assign, and connect bots unless auto_connect is off."""
        self.logger.info("manager starting with %d bots", len(self.bots))
        await self.load_proxies()
        self._assign_proxies()
        if self.config.auto_connect:
            await self.start_all()

    # ------------------------------------------------------------------ #
    # proxy loading / health / assignment
    # ------------------------------------------------------------------ #
    async def load_proxies(self) -> None:
        try:
            parsed = load_proxies_from_file(self.config.proxy_file)
        except FileNotFoundError:
            self.logger.warning("proxy file %s not found; using no proxies",
                                self.config.proxy_file)
            parsed = []
        except OSError as exc:
            self.logger.warning("cannot read %s: %s", self.config.proxy_file, exc)
            parsed = []
        # renumber sequentially
        for i, p in enumerate(parsed, start=1):
            p.index = i
        self.proxies = parsed
        self.logger.info("loaded %d proxies from %s", len(parsed), self.config.proxy_file)
        await self._health_check(parsed)

    async def _health_check(self, proxies: List[Proxy]) -> None:
        if not self.config.proxy_health_check:
            for p in proxies:
                p.status = ProxyStatus.AVAILABLE
            return
        self.logger.info("health-checking %d proxies against %s:%d",
                         len(proxies), self.config.irc.server, self.config.irc.port)
        async def _check(p: Proxy) -> None:
            ok = await check_proxy_connectivity(
                p, self.config.irc.server, self.config.irc.port,
                timeout=self.config.proxy_timeout)
            if ok:
                p.status = ProxyStatus.AVAILABLE
                self.logger.info("proxy %s healthy", p.display())
            else:
                p.status = ProxyStatus.FAILED
                p.last_error = "health check failed"
                self.logger.warning("proxy %s FAILED health check", p.display())
        await asyncio.gather(*(_check(p) for p in proxies), return_exceptions=True)

    def _assign_proxies(self) -> None:
        mode = self.config.proxy_mode
        if mode == ProxyMode.DIRECT:
            for b in self.bots.values():
                b.assign_proxy(None)
            self.logger.info("proxy_mode=direct: no proxies used")
            return

        pool = [p for p in self.proxies if p.status != ProxyStatus.FAILED
                and p.status != ProxyStatus.DISABLED]
        ordered = list(self.bots.values())  # bot001..botN

        if not pool:
            self.logger.warning("no usable proxies available")
            for b in ordered:
                if self.config.direct_when_no_proxy:
                    b.assign_proxy(None)
                    self.logger.info("%s assigned direct (no proxies)", b.bot_id)
                else:
                    b.assign_proxy(None)
            return

        if mode == ProxyMode.UNIQUE:
            for i, b in enumerate(ordered):
                if i < len(pool):
                    b.assign_proxy(pool[i])
                    self.logger.info("%s -> %s", b.bot_id, pool[i].display())
                else:
                    # not enough unique proxies
                    if self.config.direct_when_no_proxy:
                        b.assign_proxy(None)
                        self.logger.info("%s has no unique proxy; using direct",
                                         b.bot_id)
                    else:
                        b.assign_proxy(None)
                        b.status = BotStatus.OFFLINE
        else:  # ROTATE
            for i, b in enumerate(ordered):
                b.assign_proxy(pool[i % len(pool)])
                self.logger.info("%s -> %s", b.bot_id, pool[i % len(pool)].display())

    async def handle_proxy_connect_failure(self, bot: Bot) -> None:
        """Bot's proxy failed to connect: mark, re-check, and optionally reassign."""
        proxy = bot.proxy
        if proxy is None:
            return
        proxy.failures += 1
        if proxy.failures >= 2 or self.config.reassign_failed_proxy:
            proxy.status = ProxyStatus.FAILED
            proxy.last_error = f"connect failure x{proxy.failures}"
            if proxy.assigned_to == bot.bot_id:
                proxy.assigned_to = None
            self.logger.warning("proxy %s marked FAILED (bot %s)", proxy.display(),
                                bot.bot_id)
            self.logger.warning("bot %s -> searching for replacement proxy",
                                bot.bot_id)
            if self.config.reassign_failed_proxy:
                await self._try_reassign(bot)

    async def _try_reassign(self, bot: Bot) -> None:
        pool = [p for p in self.proxies
                if p.status == ProxyStatus.AVAILABLE and p.assigned_to is None]
        # ROTATE mode may also hand out proxies already assigned to others.
        if self.config.proxy_mode != ProxyMode.UNIQUE:
            pool = [p for p in self.proxies
                    if p.status == ProxyStatus.AVAILABLE]
        for cand in pool:
            if self.config.proxy_health_check:
                ok = await check_proxy_connectivity(
                    cand, self.config.irc.server, self.config.irc.port,
                    timeout=self.config.proxy_timeout)
                if not ok:
                    cand.status = ProxyStatus.FAILED
                    cand.last_error = "health check failed during reassign"
                    self.logger.warning("proxy %s FAILED during reassign check",
                                        cand.display())
                    continue
            bot.assign_proxy(cand)
            self.logger.info("bot %s reassigned -> %s", bot.bot_id, cand.display())
            return
        if self.config.fallback_direct_on_proxy_failure:
            bot.assign_proxy(None)
            self.logger.info("bot %s falling back to direct connection", bot.bot_id)
        else:
            self.logger.warning("no replacement proxy available for %s; "
                                "remaining on %s", bot.bot_id,
                                bot.proxy.display() if bot.proxy else "none")

    # ------------------------------------------------------------------ #
    # bot lookup
    # ------------------------------------------------------------------ #
    def get_bot(self, bot_id: str) -> Bot:
        if bot_id not in self.bots:
            raise CommandError(f"unknown bot {bot_id!r}")
        return self.bots[bot_id]

    def group_bots(self, group: str) -> List[Bot]:
        groups = self.config.display_groups()
        if group not in groups:
            raise CommandError(f"unknown group {group!r}")
        return [self.bots[b] for b in groups[group]]

    @property
    def connected_bots(self) -> List[Bot]:
        return [b for b in self.bots.values() if b.status == BotStatus.CONNECTED]

    @property
    def all_bots(self) -> List[Bot]:
        return list(self.bots.values())

    def _resolve(self, target: Optional[str]) -> List[Bot]:
        """Resolve an optional target id/group to a list of bots."""
        if target is None:
            return self.all_bots
        if target in self.bots:
            return [self.bots[target]]
        if target in self.config.display_groups():
            return self.group_bots(target)
        raise CommandError(f"unknown bot or group {target!r}")

    # ------------------------------------------------------------------ #
    # result gathering helper
    # ------------------------------------------------------------------ #
    @staticmethod
    def _summarize(results: List[CommandResult]) -> str:
        if not results:
            return "no bots matched"
        lines = []
        for r in results:
            mark = "✓" if r.ok else "✗"
            detail = "" if r.ok else f" {r.message}"
            lines.append(f"{r.bot_id} {mark}{detail}")
        ok = sum(1 for r in results if r.ok)
        return "\n".join(lines) + f"\n\nCompleted: {ok}/{len(results)}"

    # ------------------------------------------------------------------ #
    # single-bot lifecycle
    # ------------------------------------------------------------------ #
    async def start_bot(self, bot_id: str) -> CommandResult:
        bot = self.get_bot(bot_id)
        try:
            bot.start()
            return CommandResult(bot_id, True, "started")
        except Exception as exc:  # noqa: BLE001
            return CommandResult(bot_id, False, str(exc))

    async def stop_bot(self, bot_id: str, *, reason: str = "Stopped") -> CommandResult:
        bot = self.get_bot(bot_id)
        try:
            await bot.stop(reason=reason)
            return CommandResult(bot_id, True, "stopped")
        except Exception as exc:  # noqa: BLE001
            return CommandResult(bot_id, False, str(exc))

    async def restart_bot(self, bot_id: str) -> CommandResult:
        bot = self.get_bot(bot_id)
        try:
            await bot.stop(reason="Restarting")
            bot.start()
            return CommandResult(bot_id, True, "restarting")
        except Exception as exc:  # noqa: BLE001
            return CommandResult(bot_id, False, str(exc))

    async def quit_bot(self, bot_id: str, reason: str = "Quit") -> CommandResult:
        bot = self.get_bot(bot_id)
        try:
            if bot.connected:
                await bot.quit(reason)
            await bot.stop(reason=reason)
            return CommandResult(bot_id, True, "quit")
        except Exception as exc:  # noqa: BLE001
            return CommandResult(bot_id, False, str(exc))

    # ------------------------------------------------------------------ #
    # fleet lifecycle (all / group)
    # ------------------------------------------------------------------ #
    async def _fleet(self, bots: List[Bot], action: str, reason: str) -> List[CommandResult]:
        async def _do(bot: Bot) -> CommandResult:
            if action == "start":
                return await self.start_bot(bot.bot_id)
            if action == "stop":
                return await self.stop_bot(bot.bot_id, reason=reason)
            if action == "restart":
                return await self.restart_bot(bot.bot_id)
            if action == "quit":
                return await self.quit_bot(bot.bot_id, reason)
            return CommandResult(bot.bot_id, False, f"unknown action {action}")

        return await asyncio.gather(*(_do(b) for b in bots))

    async def start_all(self, group: Optional[str] = None) -> List[CommandResult]:
        return await self._fleet(self._resolve(group), "start", "")

    async def stop_all(self, group: Optional[str] = None,
                       reason: str = "Stopped") -> List[CommandResult]:
        return await self._fleet(self._resolve(group), "stop", reason)

    async def restart_all(self, group: Optional[str] = None) -> List[CommandResult]:
        return await self._fleet(self._resolve(group), "restart", "")

    async def quit_all(self, group: Optional[str] = None,
                       reason: str = "Quit") -> List[CommandResult]:
        return await self._fleet(self._resolve(group), "quit", reason)

    # ------------------------------------------------------------------ #
    # messaging commands
    # ------------------------------------------------------------------ #
    async def say(self, target: str, channel: str, text: str) -> List[CommandResult]:
        bots = self._resolve(target)
        targets = [b for b in bots if b.status == BotStatus.CONNECTED]
        offline = [b for b in bots if b.status != BotStatus.CONNECTED]

        async def _one(b: Bot) -> CommandResult:
            try:
                await b.say(channel, text)
                return CommandResult(b.bot_id, True, "")
            except Exception as exc:  # noqa: BLE001
                return CommandResult(b.bot_id, False, str(exc))

        results = list(await asyncio.gather(*(_one(b) for b in targets)))
        for b in offline:
            results.append(CommandResult(b.bot_id, False, "not connected"))
        return sorted(results, key=lambda r: r.bot_id)

    async def join(self, target: str, channel: str) -> List[CommandResult]:
        bots = [b for b in self._resolve(target) if b.status == BotStatus.CONNECTED]
        async def _one(b: Bot) -> CommandResult:
            try:
                await b.join_channel(channel)
                return CommandResult(b.bot_id, True, "")
            except Exception as exc:  # noqa: BLE001
                return CommandResult(b.bot_id, False, str(exc))
        return list(await asyncio.gather(*(_one(b) for b in bots)))

    async def part(self, target: str, channel: str) -> List[CommandResult]:
        bots = [b for b in self._resolve(target) if b.status == BotStatus.CONNECTED]
        async def _one(b: Bot) -> CommandResult:
            try:
                await b.part_channel(channel)
                return CommandResult(b.bot_id, True, "")
            except Exception as exc:  # noqa: BLE001
                return CommandResult(b.bot_id, False, str(exc))
        return list(await asyncio.gather(*(_one(b) for b in bots)))

    # ------------------------------------------------------------------ #
    # notification from bots
    # ------------------------------------------------------------------ #
    def notify_bot_state(self, bot: Bot) -> None:
        if self.state_callback is not None:
            try:
                self.state_callback(bot.bot_id, bot.state())
            except Exception:  # pragma: no cover
                pass

    # ------------------------------------------------------------------ #
    # status / metrics reporting
    # ------------------------------------------------------------------ #
    def status_table(self) -> str:
        rows = self._resolve(None)
        counts = {s: 0 for s in BotStatus}
        header = (f"{'ID':8} {'NICK':16} {'STATUS':12} PROXY")
        sep = "-" * 60
        lines = [header, sep]
        for b in rows:
            counts[b.status] += 1
            p = b.proxy.label if b.proxy else "direct"
            lines.append(f"{b.bot_id:8} {b.nickname:16} {b.status.value:12} {p}")
        lines.append(sep)
        lines.append(
            f"Connected: {counts[BotStatus.CONNECTED]}  "
            f"Connecting: {counts[BotStatus.CONNECTING]}  "
            f"Offline: {counts[BotStatus.OFFLINE] + counts[BotStatus.STOPPED]}  "
            f"Total: {len(rows)}"
        )
        return "\n".join(lines)

    def proxy_table(self) -> str:
        if not self.proxies:
            return "no proxies loaded (proxy_mode=%s)" % self.config.proxy_mode.value
        lines = ["PROXY STATUS", ""]
        for p in self.proxies:
            status = p.status.value
            assigned = ""
            if p.status == ProxyStatus.ASSIGNED and p.assigned_to:
                assigned = f" ASSIGNED {p.assigned_to}"
            elif p.status == ProxyStatus.ASSIGNED:
                status = ProxyStatus.AVAILABLE.value
            scheme = p.scheme.value.upper()
            lines.append(f"#{p.index:02d} {scheme:6} {p.host}:{p.port:<18} "
                         f"{status:<10}{assigned}")
        return "\n".join(lines)

    def detail(self, bot_id: str) -> str:
        bot = self.get_bot(bot_id)
        st = bot.state()
        return (
            f"ID:            {st['id']}\n"
            f"NICK:          {st['nickname']}\n"
            f"USER:          {bot.username}\n"
            f"REALNAME:      {bot.realname}\n"
            f"SERVER:        {self.config.irc.server}:{self.config.irc.port} "
            f"(tls={self.config.irc.tls})\n"
            f"PROXY:         {st['proxy']}\n"
            f"STATUS:        {st['status']}\n"
            f"CHANNELS:      {', '.join(st['channels']) if st['channels'] else '-'}\n"
            f"MSG SENT:      {st['messages_sent']}\n"
            f"MSG RECEIVED:  {st['messages_received']}\n"
            f"UPTIME:        {st['uptime']:.0f}s\n"
            f"RECONNECTS:    {st['reconnects']}\n"
            f"LAST ERROR:    {st['last_error'] or '-'}"
        )

    def summary(self) -> str:
        counts = {s: 0 for s in BotStatus}
        for b in self.bots.values():
            counts[b.status] += 1
        total = len(self.bots)
        c = counts
        return (f"Bots configured: {total}   Connected: {c[BotStatus.CONNECTED]}  "
                f"Connecting: {c[BotStatus.CONNECTING]}  "
                f"Offline: {c[BotStatus.OFFLINE] + c[BotStatus.STOPPED]}")

    # ------------------------------------------------------------------ #
    # reload
    # ------------------------------------------------------------------ #
    async def reload_proxies(self) -> str:
        """Re-read proxy.txt preserving healthy assignments where possible."""
        before_keys = {_key(p): p for p in self.proxies}
        try:
            parsed = load_proxies_from_file(self.config.proxy_file)
        except (FileNotFoundError, OSError) as exc:
            return f"cannot reload proxies: {exc}"
        for i, p in enumerate(parsed, start=1):
            p.index = i
        self.proxies = parsed
        await self._health_check(parsed)

        if self.config.proxy_mode == ProxyMode.DIRECT:
            return "direct mode: proxy reload ignored"

        new_keys = {_key(p) for p in parsed}
        # 1) keep assignments to proxies that still exist
        kept: set = set()
        for b in self.bots.values():
            if b.proxy is None:
                continue
            key = _key(b.proxy)
            if key in new_keys and b.proxy.status != ProxyStatus.FAILED:
                # map bot to the freshly loaded matching proxy object
                for np in parsed:
                    if _key(np) == key:
                        b.assign_proxy(np)
                        break
                kept.add(key)

        # 2) newly available proxies (not kept & available)
        available = [p for p in parsed if p.status == ProxyStatus.AVAILABLE
                     and p not in [b.proxy for b in self.bots.values()]]

        # 3) bots that lost their proxy -> reassign if possible
        lost = [b for b in self.bots.values()
                if b.proxy is None or _key(b.proxy) not in new_keys]
        # clear assignment markers of removed proxies
        for p in before_keys.values():
            if _key(p) not in new_keys and p.assigned_to:
                p.assigned_to = None
        for b in lost:
            if b.proxy is not None and _key(b.proxy) not in new_keys:
                b.proxy = None
        if available:
            used = set()
            for b in lost:
                if self.config.proxy_mode == ProxyMode.UNIQUE:
                    cand = next((p for p in available
                                 if _key(p) not in used), None)
                else:
                    cand = available[(lost.index(b)) % len(available)]
                if cand is not None:
                    b.assign_proxy(cand)
                    used.add(_key(cand))
                    self.logger.info("%s reassigned -> %s (reload)", b.bot_id,
                                     cand.display())
        return self.proxy_table()

    async def reload_config(self, new_config: AppConfig) -> str:
        """Apply a freshly loaded config.

        If the requested bot count differs, the fleet is *not* resized at
        runtime (that would be disruptive); the new operational settings are
        applied and a warning is returned.  Proxy assignments are refreshed
        for the new ``proxy_mode``.
        """
        if new_config.bot_count != self.config.bot_count:
            self.logger.warning(
                "bot_count changed (%d -> %d); not resizing a running fleet",
                self.config.bot_count, new_config.bot_count)
            new_config.bot_count = self.config.bot_count
        old_mode = self.config.proxy_mode
        self.config = new_config
        for b in self.bots.values():
            b.config = new_config
            from utils import TokenBucket
            b._rate = TokenBucket(new_config.rate_limit.messages_per_second,
                                  new_config.rate_limit.burst)
        self.logger.info("configuration reloaded")
        note = ""
        if new_config.proxy_mode != old_mode:
            self._assign_proxies()
            note = "\nproxy_mode changed; assignments refreshed"
        return f"configuration reloaded{note}"

    # ------------------------------------------------------------------ #
    # shutdown
    # ------------------------------------------------------------------ #
    async def shutdown(self, reason: str = "Manager shutting down") -> None:
        await self.stop_all(reason=reason)
        self.logger.info("manager shutdown complete")
