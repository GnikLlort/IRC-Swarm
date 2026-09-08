"""A single managed bot.

The :class:`Bot` owns the lifecycle of one :class:`IrcClient`, transparently
reconnects with exponential backoff, tracks its own state and counters, joins
channels once registered, throttles outbound chat, and answers a small set of
*authorized, safe* channel commands (``!ping``, ``!uptime``, ``!info``).

A bot is an independent asyncio task: it can be cancelled/stopped on its own
without affecting any other bot or the manager.
"""

from __future__ import annotations

import asyncio
import time
from typing import List, Optional, TYPE_CHECKING

from config import AppConfig
from irc_client import (IrcClient, MESSAGE, NOTICE, JOIN, PART, READY,
                        NUMERIC, KICK)
from models import (BotStatus, CommandResult, IrcMessage, Proxy, ProxyStatus)
from utils import TokenBucket, format_duration

if TYPE_CHECKING:  # pragma: no cover
    from manager import BotManager

from logger import register_secret


class Bot:
    def __init__(self, bot_id: str, config: AppConfig,
                 manager: Optional["BotManager"] = None, *, logger=None) -> None:
        self.bot_id = bot_id
        self.config = config
        self.manager = manager
        self.nickname = config.nick_for(bot_id)
        self.username = config.irc.username
        self.realname = config.irc.realname
        self.proxy: Optional[Proxy] = None

        self.status = BotStatus.STOPPED
        self.channels: set = set()
        self.stats = CounterStats()
        self.reconnect_count = 0
        self.last_error = ""
        self.started_at: Optional[float] = None
        self.connected_at: Optional[float] = None
        self._backoff_delay = 0.0
        self._consecutive_failures = 0

        self.logger = logger or _mk_logger(bot_id)
        self._client: Optional[IrcClient] = None
        self._task: Optional[asyncio.Task] = None
        self._stop_event = asyncio.Event()
        self._rate = TokenBucket(
            config.rate_limit.messages_per_second,
            config.rate_limit.burst,
        )
        if config.irc.password:
            register_secret(config.irc.password)

    # ------------------------------------------------------------------ #
    # public actions
    # ------------------------------------------------------------------ #
    def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop_event.clear()
        self.status = BotStatus.CONNECTING
        self.started_at = time.monotonic()
        self.logger.info("%s starting", self.bot_id)
        self._task = asyncio.ensure_future(self._run_loop())
        self._notify_state()

    async def stop(self, *, reason: str = "Stopped by manager") -> None:
        self._stop_event.set()
        if self._client is not None:
            try:
                await self._client.request_quit(reason)
            except Exception:  # noqa: BLE001
                pass
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001
                self.logger.exception("error stopping %s", self.bot_id)
            self._task = None
        self.status = BotStatus.STOPPED
        self._notify_state()

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def connected(self) -> bool:
        return self.status == BotStatus.CONNECTED

    @property
    def uptime(self) -> float:
        base = self.started_at or time.monotonic()
        return time.monotonic() - base

    # ------------------------------------------------------------------ #
    # reconnect orchestration
    # ------------------------------------------------------------------ #
    async def _run_loop(self) -> None:
        try:
            while not self._stop_event.is_set():
                try:
                    await self._connect_once()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - isolation
                    self.logger.exception("unhandled error in %s loop", self.bot_id)
                    self.last_error = str(exc)
                if self._stop_event.is_set():
                    break
                await self._prepare_reconnect()
            # while loop exits -> stopped
            if self._stop_event.is_set():
                self.status = BotStatus.STOPPED
            else:
                self.status = BotStatus.OFFLINE
        except asyncio.CancelledError:
            self.status = BotStatus.STOPPED
            self._stop_event.set()
            raise
        finally:
            self._notify_state()
            self.logger.info("%s task ended (status=%s)", self.bot_id, self.status.value)

    async def _connect_once(self) -> None:
        client = self._make_client()
        self._client = client
        self.status = BotStatus.CONNECTING
        self._notify_state()
        self.logger.info("%s connecting (proxy=%s)", self.bot_id,
                         self.proxy.display() if self.proxy else "direct")
        self._attach_handlers(client)
        reason = await client.run()

        if reason == "connect_error":
            self._consecutive_failures += 1
            self.logger.warning("%s connect failed (proxy=%s)", self.bot_id,
                                self.proxy.display() if self.proxy else "direct")
            if self.proxy is not None and self.manager is not None:
                await self.manager.handle_proxy_connect_failure(self)
            elif self.proxy is not None:
                # no manager (tests) -> mark proxy failed locally after repeats
                if self._consecutive_failures >= 2:
                    self.proxy.status = ProxyStatus.FAILED
            return

        self._consecutive_failures = 0
        self._backoff_delay = 0.0
        if reason == "quit":
            self.logger.info("%s quit", self.bot_id)
        elif reason == "remote_close":
            self.logger.info("%s server closed connection", self.bot_id)
        elif reason == "session_error":
            self.logger.warning("%s session error", self.bot_id)
            if self.proxy is not None and self.manager is not None:
                await self.manager.handle_proxy_connect_failure(self)

    async def _prepare_reconnect(self) -> None:
        self.status = BotStatus.RECONNECTING
        self._notify_state()
        delay = self._next_backoff()
        self.reconnect_count += 1
        self.logger.info("%s reconnecting in %.0fs (attempt %d)",
                         self.bot_id, delay, self.reconnect_count)
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=delay)
        except asyncio.TimeoutError:
            pass
        except asyncio.CancelledError:
            raise

    def _next_backoff(self) -> float:
        rc = self.config.reconnect
        if self._backoff_delay <= 0:
            self._backoff_delay = rc.initial_delay
        else:
            self._backoff_delay = min(self._backoff_delay * rc.factor, rc.max_delay)
        return self._backoff_delay

    def _make_client(self) -> IrcClient:
        return IrcClient(
            host=self.config.irc.server,
            port=self.config.irc.port,
            nickname=self.nickname,
            username=self.username,
            realname=self.realname,
            password=self.config.irc.password,
            tls=self.config.irc.tls,
            verify_tls=self.config.irc.verify_tls,
            proxy=self.proxy,
            max_line_length=self.config.max_line_length,
            connect_timeout=self.config.connect_timeout,
            logger=self.logger,
        )

    # ------------------------------------------------------------------ #
    # events
    # ------------------------------------------------------------------ #
    def _attach_handlers(self, client: IrcClient) -> None:
        client.on(READY, self._on_ready)
        client.on(MESSAGE, self._on_message)
        client.on(NOTICE, self._on_notice)
        client.on(JOIN, self._on_join)
        client.on(PART, self._on_part)
        client.on(KICK, self._on_kick)
        client.on(NUMERIC, self._on_numeric)

    async def _on_ready(self, client: IrcClient) -> None:
        self.status = BotStatus.CONNECTED
        self.connected_at = time.monotonic()
        self._consecutive_failures = 0
        self.last_error = ""
        self.logger.info("%s registered as %s", self.bot_id, client.nickname)
        for ch in self.config.irc.channels:
            await self.join_channel(ch, autostart=True)
        self._notify_state()

    async def _on_message(self, client: IrcClient, msg: IrcMessage) -> None:
        self.stats.messages_received += 1
        await self._maybe_handle_command(client, msg)

    async def _on_notice(self, client: IrcClient, msg: IrcMessage) -> None:
        self.stats.messages_received += 1

    async def _on_join(self, client: IrcClient, msg: IrcMessage) -> None:
        sender = msg.sender_nick
        channel = msg.param(0)
        if sender.lower() == client.nickname.lower():
            self.channels.add(channel)
            self.logger.info("%s joined %s", self.bot_id, channel)

    async def _on_part(self, client: IrcClient, msg: IrcMessage) -> None:
        sender = msg.sender_nick
        channel = msg.param(0)
        if sender.lower() == client.nickname.lower():
            self.channels.discard(channel)
            self.logger.info("%s parted %s", self.bot_id, channel)

    async def _on_kick(self, client: IrcClient, msg: IrcMessage) -> None:
        channel = msg.param(0)
        victim = msg.param(1)
        if victim.lower() == client.nickname.lower():
            self.channels.discard(channel)
            self.logger.warning("%s was kicked from %s", self.bot_id, channel)
            # rejoin after a short moment unless stopping
            if not self._stop_event.is_set():
                await asyncio.sleep(1)
                await self.join_channel(channel, autostart=True)

    async def _on_numeric(self, client: IrcClient, msg: IrcMessage) -> None:
        if msg.command == "433" or msg.command == "436":
            # collision already handled at client level for re-nick
            pass

    # ------------------------------------------------------------------ #
    # outbound commands
    # ------------------------------------------------------------------ #
    async def join_channel(self, channel: str, autostart: bool = False) -> None:
        if self._client is None or not self._client.connected:
            raise ConnectionError(f"{self.bot_id}: not connected")
        await self._client.join(channel)
        self.logger.info("%s JOIN %s", self.bot_id, channel)

    async def part_channel(self, channel: str, reason: str = "") -> None:
        if self._client is None or not self._client.connected:
            raise ConnectionError(f"{self.bot_id}: not connected")
        await self._client.part(channel, reason)
        self.channels.discard(channel)
        self.logger.info("%s PART %s", self.bot_id, channel)

    async def say(self, channel: str, text: str) -> None:
        if self._client is None or not self._client.connected:
            raise ConnectionError(f"{self.bot_id}: not connected")
        await self._rate.acquire()
        await self._client.send_privmsg(channel, text)
        self.stats.messages_sent += 1
        self.logger.info("%s -> %s: %s", self.bot_id, channel, _clip(text))

    async def notice(self, target: str, text: str) -> None:
        if self._client is None or not self._client.connected:
            raise ConnectionError(f"{self.bot_id}: not connected")
        await self._rate.acquire()
        await self._client.send_notice(target, text)
        self.stats.messages_sent += 1

    async def quit(self, reason: str = "Leaving") -> None:
        if self._client is not None and self._client.connected:
            await self._client.request_quit(reason)
        self.status = BotStatus.QUITTING
        self._notify_state()

    # ------------------------------------------------------------------ #
    # safe channel command framework (authorized testing only)
    # ------------------------------------------------------------------ #
    async def _maybe_handle_command(self, client: IrcClient, msg: IrcMessage) -> None:
        target = msg.param(0, "")
        sender = msg.sender_nick
        text = msg.text
        if not text.startswith("!"):
            return
        if sender.lower() == client.nickname.lower():
            return  # never act on our own echo
        # decide where to reply
        if target.startswith(("#", "&", "+", "!")):
            reply_target = target
        else:
            reply_target = sender  # private message

        parts = text.split()
        cmd = parts[0].lower()
        if cmd == "!ping":
            await self.say(reply_target, "pong")
        elif cmd == "!uptime":
            await self.say(reply_target, f"up {format_duration(self.uptime)}")
        elif cmd == "!info":
            await self.say(reply_target, self._info_line())
        # any other !command is ignored - never executed

    def _info_line(self) -> str:
        return (
            f"[{self.bot_id}] nick={self.nickname} server={self.config.irc.server} "
            f"status={self.status.value} proxy={self.proxy.display() if self.proxy else 'direct'} "
            f"msgs_sent={self.stats.messages_sent} msgs_recv={self.stats.messages_received} "
            f"uptime={format_duration(self.uptime)}"
        )

    # ------------------------------------------------------------------ #
    # state / metrics
    # ------------------------------------------------------------------ #
    def assign_proxy(self, proxy: Optional[Proxy]) -> None:
        if self.proxy is not None and self.proxy is not proxy:
            if self.proxy.assigned_to == self.bot_id:
                self.proxy.assigned_to = None
                if self.proxy.status == ProxyStatus.ASSIGNED:
                    self.proxy.status = ProxyStatus.AVAILABLE
        self.proxy = proxy
        if proxy is not None:
            proxy.assigned_to = self.bot_id
            proxy.status = ProxyStatus.ASSIGNED

    def state(self) -> dict:
        return {
            "id": self.bot_id,
            "nickname": self.nickname,
            "status": self.status.value,
            "proxy": self.proxy.display() if self.proxy else "direct",
            "proxy_index": self.proxy.index if self.proxy else None,
            "connected": self.connected,
            "channels": sorted(self.channels),
            "messages_sent": self.stats.messages_sent,
            "messages_received": self.stats.messages_received,
            "uptime": self.uptime,
            "reconnects": self.reconnect_count,
            "last_error": self.last_error,
        }

    def _notify_state(self) -> None:
        if self.manager is not None:
            try:
                self.manager.notify_bot_state(self)
            except Exception:  # pragma: no cover
                pass

    def summary(self) -> str:
        p = self.proxy.display() if self.proxy else "direct"
        return (f"{self.bot_id:8} {self.nickname:16} {self.status.value:12} "
                f"{p}  up={format_duration(self.uptime)} reconnects={self.reconnect_count}")


class CounterStats:
    """Plain counters (avoids circular import of models.BotStats dataclass)."""

    def __init__(self) -> None:
        self.messages_sent = 0
        self.messages_received = 0


def _clip(text: str, limit: int = 300) -> str:
    text = text.replace("\n", " ").replace("\r", " ")
    return text if len(text) <= limit else text[:limit] + "…"


def _mk_logger(bot_id: str):
    from logger import facility
    return facility().bot_logger(bot_id)
