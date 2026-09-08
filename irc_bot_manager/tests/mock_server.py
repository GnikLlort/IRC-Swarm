"""A lightweight in-process mock IRC server used by the test-suite.

It supports multiple simultaneous connections, NICK/USER registration,
PING/PONG, JOIN, PRIVMSG, client-side disconnect (for reconnection tests) and
server->client message injection.  No public IRC network is ever contacted.
"""

from __future__ import annotations

import asyncio
from typing import List, Optional


def _split_irc(line: str) -> tuple:
    rest = line
    prefix = ""
    if rest.startswith(":"):
        sp = rest.find(" ")
        prefix, rest = rest[1:sp], rest[sp + 1:] if sp != -1 else ""
    sp = rest.find(" ")
    if sp == -1:
        return prefix, rest, []
    command = rest[:sp]
    params = rest[sp + 1:]
    out = []
    while params:
        params = params.lstrip()
        if params.startswith(":"):
            out.append(params[1:])
            break
        sp = params.find(" ")
        if sp == -1:
            out.append(params)
            break
        out.append(params[:sp])
        params = params[sp + 1:]
    return prefix, command, out


class MockClient:
    def __init__(self, reader, writer, server, cid: int):
        self.reader = reader
        self.writer = writer
        self.server = server
        self.cid = cid
        self.nick: Optional[str] = None
        self.registered = False
        self.joined: set = set()
        self.lines: List[str] = []       # every raw line the client sent
        self.messages: List[str] = []    # PRIVMSG text the client sent
        self.privmsg_to: List[tuple] = []
        self.closed = False

    async def send(self, line: str) -> None:
        if self.writer.is_closing():
            return
        self.writer.write((line + "\r\n").encode("utf-8", "replace"))
        await self.writer.drain()

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            self.writer.close()
            await self.writer.wait_closed()
        except Exception:
            pass


class MockServer:
    def __init__(self):
        self.clients: List[MockClient] = []
        self.server = None
        self.port = None
        self.host = "127.0.0.1"
        self._cid = 0

    @property
    def address(self) -> tuple:
        return self.host, self.port

    async def start(self) -> None:
        self.server = await asyncio.start_server(self._handle, self.host, 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        for c in list(self.clients):
            await c.close()
        self.clients.clear()
        if self.server is not None:
            self.server.close()
            try:
                await self.server.wait_closed()
            except Exception:
                pass

    async def _handle(self, reader, writer) -> None:
        cid = self._cid
        self._cid += 1
        client = MockClient(reader, writer, self, cid)
        self.clients.append(client)
        try:
            await self._read_loop(client)
        except asyncio.CancelledError:
            pass
        except Exception:
            pass
        finally:
            client.closed = True
            if client in self.clients:
                self.clients.remove(client)

    async def _read_loop(self, client: MockClient) -> None:
        buf = bytearray()
        while True:
            data = await client.reader.read(4096)
            if not data:
                return
            buf += data
            while True:
                nl = buf.find(b"\n")
                if nl == -1:
                    if len(buf) > 4096:
                        buf.clear()
                    break
                raw = bytes(buf[:nl]).rstrip(b"\r")
                del buf[: nl + 1]
                if raw:
                    await self._process(client, raw.decode("utf-8", "replace"))

    async def _process(self, client: MockClient, line: str) -> None:
        client.lines.append(line)
        _prefix, cmd, params = _split_irc(line)
        cmd = cmd.upper()

        if cmd == "NICK":
            client.nick = (params[0] if params else "?")
            await self._maybe_register(client)
        elif cmd == "USER":
            client.real = "user"
            await self._maybe_register(client)
        elif cmd == "PING":
            await client.send(f":server PONG :{params[0] if params else 'x'}")
        elif cmd == "JOIN":
            ch = params[0] if params else "#x"
            client.joined.add(ch)
            await client.send(f":{client.nick}!u@h JOIN :{ch}")
            await client.send(f":server 353 {client.nick} = {ch} :@{client.nick}")
            await client.send(f":server 366 {client.nick} {ch} :End of NAMES")
        elif cmd == "PART":
            ch = params[0] if params else "#x"
            client.joined.discard(ch)
            await client.send(f":{client.nick}!u@h PART {ch}")
        elif cmd == "PRIVMSG":
            target = params[0] if params else ""
            text = params[1] if len(params) > 1 else ""
            client.messages.append(text)
            client.privmsg_to.append((target, text))
        elif cmd == "QUIT":
            await client.send(f"ERROR :Closing Link: {client.nick} (Quit)")
            await client.close()
            return
        # CAP, USER, PONG, NOTICE ... ignored
        if cmd in ("USER",):
            pass

    async def _maybe_register(self, client: MockClient) -> None:
        if client.nick and not client.registered:
            client.registered = True
            nick = client.nick
            await client.send(f":server 001 {nick} :Welcome to the mock network")
            await client.send(f":server 002 {nick} :Your host is mock")
            await client.send(f":server 376 {nick} :End of MOTD")
            await client.send(f"PING :mock{client.cid}")

    # ------------------------------------------------------------- #
    # helpers used by tests
    # ------------------------------------------------------------- #
    def client_by_nick(self, nick: str) -> Optional[MockClient]:
        for c in self.clients:
            if c.nick and c.nick.lower() == nick.lower():
                return c
        return None

    async def say_as(self, nick: str, target_nick: str, text: str) -> bool:
        """Inject a PRIVMSG from a fake tester to ``target_nick``."""
        c = self.client_by_nick(target_nick)
        if c is None:
            return False
        await c.send(f":tester!u@h PRIVMSG {target_nick} :{text}")
        return True

    async def push(self, nick: str, line: str) -> bool:
        """Inject an arbitrary raw line to a connected client by nick."""
        c = self.client_by_nick(nick)
        if c is None:
            return False
        await c.send(line)
        return True

    async def drop_client(self, nick: str) -> None:
        c = self.client_by_nick(nick)
        if c is not None:
            await c.close()

    async def drop_all(self) -> None:
        for c in list(self.clients):
            await c.close()

    async def send_kick(self, channel: str, target_nick: str) -> None:
        c = self.client_by_nick(target_nick)
        if c is not None:
            await c.send(f":op!u@h KICK {channel} {target_nick} :bye")

    def privmsg_texts(self, nick: Optional[str] = None) -> List[str]:
        c = self.client_by_nick(nick) if nick else None
        if c is not None:
            return c.messages
        texts = []
        for c in self.clients:
            texts.extend(c.messages)
        return texts
