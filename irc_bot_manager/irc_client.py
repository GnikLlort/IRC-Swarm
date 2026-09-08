"""Asynchronous IRC protocol client.

Implements a single bot's connection to one IRC server: buffered reading,
line parsing, TLS / proxy streaming, registration, PING/PONG, basic CAP
negotiation, numeric handling and auto re-nick on collision.  All low level
and semantic events are published on an internal :class:`EventEmitter`.

Data received from the server is *never* trusted and never executed.
"""

from __future__ import annotations

import asyncio
import logging
import ssl
from typing import List, Optional

from models import IrcMessage, Proxy
from proxy import open_proxy_tunnel
from utils import EventEmitter, safe_nick_suffix

log = logging.getLogger("irc_client")

CONNECT = "connect"
DISCONNECT = "disconnect"
READY = "ready"
RAW = "raw_message"
MESSAGE = "message"          # PRIVMSG received
NOTICE = "notice"
JOIN = "join"
PART = "part"
QUIT = "quit"
KICK = "kick"
NICK = "nick"
ERROR = "error"
NUMERIC = "numeric"
PING = "ping"

# nick collision / not available numerics
_NICK_UNAVAILABLE = {"432", "433", "436", "437"}


class ConnectionClosed(Exception):
    """Raised internally when the server side closes the connection."""


class IrcClient:
    """One independent, cancellable IRC connection."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        nickname: str,
        username: str = "admin",
        realname: str = "IRC Bot Manager",
        password: Optional[str] = None,
        tls: bool = True,
        verify_tls: bool = True,
        proxy: Optional[Proxy] = None,
        max_line_length: int = 512,
        connect_timeout: float = 10.0,
        logger: logging.Logger = log,
    ) -> None:
        self.host = host
        self.port = port
        self.nickname = nickname
        self.username = username
        self.realname = realname
        self.password = password
        self.tls = tls
        self.verify_tls = verify_tls
        self.proxy = proxy
        self.max_line_length = max_line_length
        self.connect_timeout = connect_timeout
        self.logger = logger

        self.events = EventEmitter()

        self.reader: Optional[asyncio.StreamReader] = None
        self.writer: Optional[asyncio.StreamWriter] = None
        self.connected = False
        self.registered = False
        self._stop_requested = False
        self._write_lock = asyncio.Lock()
        self._ssl_context: Optional[ssl.SSLContext] = None

    # ------------------------------------------------------------------ #
    # event convenience
    # ------------------------------------------------------------------ #
    def on(self, event: str, handler) -> None:
        self.events.on(event, handler)

    # ------------------------------------------------------------------ #
    # transport
    # ------------------------------------------------------------------ #
    def _make_ssl_context(self) -> ssl.SSLContext:
        if self._ssl_context is None:
            ctx = ssl.create_default_context()
            if not self.verify_tls:
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
            self._ssl_context = ctx
        return self._ssl_context

    async def _open_stream(self) -> tuple:
        """Open (and if needed TLS-wire) the stream to the IRC server."""
        host, port = self.host, self.port
        if self.tls and not self.proxy:
            return await asyncio.open_connection(
                host, port, ssl=self._make_ssl_context(), server_hostname=host
            )

        if self.proxy is not None:
            reader, writer = await open_proxy_tunnel(
                self.proxy, host, port, timeout=self.connect_timeout
            )
            if self.tls:
                writer = await writer.start_tls(
                    self._make_ssl_context(), server_hostname=host,
                    ssl_handshake_timeout=self.connect_timeout,
                )
            return reader, writer

        # plaintext direct
        return await asyncio.open_connection(host, port)

    async def _connect_transport(self) -> None:
        if self.proxy is not None:
            self.logger.info("connecting via %s to %s:%d (tls=%s)",
                             self.proxy.display(), self.host, self.port, self.tls)
        else:
            self.logger.info("connecting direct to %s:%d (tls=%s)",
                             self.host, self.port, self.tls)
        try:
            reader, writer = await asyncio.wait_for(
                self._open_stream(), timeout=self.connect_timeout
            )
        except asyncio.TimeoutError as exc:
            raise ConnectionError(f"connection to {self.host}:{self.port} timed out") from exc
        self.reader, self.writer = reader, writer
        self.connected = True

    # ------------------------------------------------------------------ #
    # public lifecycle
    # ------------------------------------------------------------------ #
    async def run(self) -> str:
        """Connect, register and read until disconnect.

        Returns a reason string: ``"quit"``, ``"remote_close"``, ``"error"``
        or ``"disconnect"``.  Bot owns reconnection/backoff around this call.
        """
        self._stop_requested = False
        await self._connect_transport()
        await self.events.emit(CONNECT, self)
        try:
            await self._register()
            # Real registration is confirmed by the server's numeric 001,
            # which sets self.registered and emits READY exactly once.
            await self._read_loop()
            if self._stop_requested:
                return "quit"
            return "remote_close"
        except ConnectionClosed:
            return "remote_close"
        except asyncio.CancelledError:
            self.logger.info("%s cancelled", self.nickname)
            raise
        except Exception as exc:  # noqa: BLE001
            self.logger.error("%s connection error: %s", self.nickname, exc)
            return "error"
        finally:
            await self._close_transport()

    async def request_quit(self, reason: str = "Leaving") -> None:
        """Politely QUIT and close. Idempotent & safe to call anytime."""
        self._stop_requested = True
        if self.connected and self.writer is not None:
            try:
                await self.send_raw(f"QUIT :{reason}")
            except Exception:  # noqa: BLE001
                pass
            self.connected = False
            await self._close_transport()

    async def _close_transport(self) -> None:
        was = self.connected
        self.connected = False
        self.registered = False
        writer, self.writer = self.writer, None
        self.reader = None
        if writer is not None:
            try:
                writer.close()
                try:
                    await writer.wait_closed()
                except Exception:
                    pass
            except Exception:  # noqa: BLE001
                pass
        if was:
            await self.events.emit(DISCONNECT, self)

    # ------------------------------------------------------------------ #
    # registration
    # ------------------------------------------------------------------ #
    async def _register(self) -> None:
        if self.password:
            await self.send_raw(f"PASS :{self.password}")
        # Basic CAP negotiation: advertise 302, NICK/USER right away.
        await self.send_raw("CAP LS 302")
        await self.send_raw(f"NICK :{self.nickname}")
        await self.send_raw(f"USER {self.username} 0 * :{self.realname}")

    # ------------------------------------------------------------------ #
    # sending
    # ------------------------------------------------------------------ #
    async def send_raw(self, line: str) -> None:
        """Send one IRC line, enforcing the max line length and write lock."""
        if not self.connected or self.writer is None:
            raise ConnectionError(f"{self.nickname}: not connected")
        line = line.rstrip("\r\n")
        payload = line.encode("utf-8", "replace")
        if len(payload) > self.max_line_length:
            raise ValueError(f"outgoing line exceeds {self.max_line_length} bytes")
        async with self._write_lock:
            self.writer.write(payload + b"\r\n")
            await self.writer.drain()

    async def send_privmsg(self, target: str, text: str) -> None:
        await self.send_raw(f"PRIVMSG {target} :{text}")

    async def send_notice(self, target: str, text: str) -> None:
        await self.send_raw(f"NOTICE {target} :{text}")

    async def join(self, channel: str) -> None:
        await self.send_raw(f"JOIN {channel}")

    async def part(self, channel: str, reason: str = "") -> None:
        if reason:
            await self.send_raw(f"PART {channel} :{reason}")
        else:
            await self.send_raw(f"PART {channel}")

    async def nick(self, new_nick: str) -> None:
        self.nickname = new_nick
        await self.send_raw(f"NICK :{new_nick}")

    # ------------------------------------------------------------------ #
    # reading / parsing
    # ------------------------------------------------------------------ #
    async def _read_loop(self) -> None:
        assert self.reader is not None
        buf = bytearray()
        while True:
            try:
                data = await self.reader.read(4096)
            except (ConnectionError, OSError, ssl.SSLError) as exc:
                self.logger.warning("%s read error: %s", self.nickname, exc)
                return
            if not data:
                return  # remote closed
            buf += data

            while True:
                nl = buf.find(b"\n")
                if nl == -1:
                    # protect against unbounded memory from a hostile stream
                    if len(buf) > self.max_line_length + 1024:
                        self.logger.warning(
                            "%s discarding oversized/undelimited stream data", self.nickname)
                        buf.clear()
                    break
                raw = bytes(buf[:nl]).rstrip(b"\r")
                del buf[: nl + 1]
                if raw:
                    try:
                        text = raw.decode("utf-8", "replace")
                    except Exception:  # pragma: no cover
                        text = raw.decode("latin1", "replace")
                    if len(text) > self.max_line_length:
                        self.logger.warning("%s ignoring overlong line (%d bytes)",
                                            self.nickname, len(text))
                        continue
                    await self._process_line(text)

    async def _process_line(self, line: str) -> None:
        msg = parse_irc_line(line)
        if msg is None:
            self.logger.debug("%s malformed line ignored", self.nickname)
            await self.events.emit(ERROR, self, code="PARSE", text="malformed line")
            return
        await self.events.emit(RAW, self, msg)
        await self._route(msg)

    async def _route(self, msg: IrcMessage) -> None:
        cmd = msg.command.upper()
        params = msg.params

        if cmd == "PING":
            token = " ".join(params)
            await self.send_raw(f"PONG :{token}" if token else "PONG")
            await self.events.emit(PING, self, token)
            return

        if cmd == "CAP" and params:
            sub = params[1].upper() if len(params) > 1 else ""
            if sub in ("LS", "ACK", "NAK") and not self._stop_requested:
                # We requested no optional caps; end negotiation promptly.
                await self.send_raw("CAP END")
            return

        if cmd.isdigit() or (len(cmd) == 3 and cmd.isdigit()):
            await self.events.emit(NUMERIC, self, msg)
            await self._handle_numeric(msg)
            return

        # high level commands -------------------------------------------
        if cmd == "PRIVMSG":
            await self.events.emit(MESSAGE, self, msg)
        elif cmd == "NOTICE":
            await self.events.emit(NOTICE, self, msg)
        elif cmd == "JOIN":
            await self.events.emit(JOIN, self, msg)
        elif cmd == "PART":
            await self.events.emit(PART, self, msg)
        elif cmd == "QUIT":
            await self.events.emit(QUIT, self, msg)
        elif cmd == "KICK":
            await self.events.emit(KICK, self, msg)
        elif cmd == "NICK":
            await self.events.emit(NICK, self, msg)

    async def _handle_numeric(self, msg: IrcMessage) -> None:
        code = msg.command
        if code == "001":
            self.registered = True
            if not getattr(self, "_ready_emitted", False):
                self._ready_emitted = True
                await self.events.emit(READY, self)
            return

        if code in _NICK_UNAVAILABLE:
            old = self.nickname
            new = f"{old}_{safe_nick_suffix()}"
            self.logger.warning("%s nick unavailable (%s), retrying as %s",
                                old, code, new)
            try:
                await self.nick(new)
            except Exception:  # noqa: BLE001
                pass

        if code in {"464", "465"}:
            self.logger.error("server rejected us (%s)", code)
            await self.events.emit(ERROR, self, code=code,
                                   text=msg.text or msg.command)

    # convenience used by handlers to reply to a channel
    def fmt(self, channel: str) -> str:
        return channel


def parse_irc_line(line: str) -> Optional[IrcMessage]:
    """Parse a single raw IRC line into an :class:`IrcMessage`.

    Returns ``None`` for input that cannot form a valid IRC message.  Tolerates
    IRCv3 tags and optional leading prefix.  Never raises.
    """
    if not line:
        return None
    rest = line.rstrip("\r\n")
    if rest.startswith("@"):
        sp = rest.find(" ")
        if sp == -1:
            return None
        rest = rest[sp + 1:]

    prefix = ""
    if rest.startswith(":"):
        sp = rest.find(" ")
        if sp == -1:
            return None
        prefix = rest[1:sp]
        rest = rest[sp + 1:]
        if not rest:
            return None

    rest = rest.lstrip()
    sp = rest.find(" ")
    if sp == -1:
        command = rest
        rest = ""
    else:
        command = rest[:sp]
        rest = rest[sp + 1:]

    if not command:
        return None

    params: List[str] = []
    has_trailing = False
    remaining = rest
    while remaining:
        remaining = remaining.lstrip(" ")
        if not remaining:
            break
        if remaining.startswith(":"):
            params.append(remaining[1:])  # trailing keeps internal spaces
            has_trailing = True
            break
        nxt = remaining.find(" ")
        if nxt == -1:
            params.append(remaining)
            break
        params.append(remaining[:nxt])
        remaining = remaining[nxt + 1:]

    return IrcMessage(raw=line.rstrip("\r\n"), prefix=prefix,
                      command=command, params=params,
                      has_trailing=has_trailing)
