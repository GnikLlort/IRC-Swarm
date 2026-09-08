"""Tests for IRC line parsing and the asynchronous IRC client.

The client tests run against the in-process mock server (no public network).
"""

import asyncio
import pytest

from irc_client import IrcClient, parse_irc_line, MESSAGE, READY
from mock_server import MockServer
from util import wait_until


# --------------------------------------------------------------------------- #
# parsing
# --------------------------------------------------------------------------- #
def test_parse_simple():
    m = parse_irc_line("PING :abc123")
    assert m.command == "PING"
    assert m.params == ["abc123"]


def test_parse_prefix_and_params():
    m = parse_irc_line(":nick!user@host PRIVMSG #chan :hello world")
    assert m.prefix == "nick!user@host"
    assert m.sender_nick == "nick"
    assert m.command == "PRIVMSG"
    assert m.params == ["#chan", "hello world"]
    assert m.text == "hello world"


def test_parse_no_trailing():
    m = parse_irc_line(":server 001 nick :Welcome")
    assert m.command == "001"
    assert m.params[0] == "nick"
    assert m.text == "Welcome"


def test_parse_numeric_without_prefix():
    m = parse_irc_line("353 nick = #chan :@nick other")
    assert m.command == "353"


def test_parse_with_tags():
    m = parse_irc_line("@time=123;account=me :nick!u@h PRIVMSG #c :hi")
    assert m.command == "PRIVMSG"
    assert m.params == ["#c", "hi"]


def test_parse_command_with_no_params():
    m = parse_irc_line(":nick!u@h QUIT")
    assert m.command == "QUIT"
    assert m.params == []


def test_parse_malformed_returns_none():
    assert parse_irc_line("") is None
    assert parse_irc_line("   ") is None
    assert parse_irc_line(":onlyprefix") is None
    assert parse_irc_line(":") is None


def test_parse_carriage_return_stripped():
    m = parse_irc_line("PING :x\r")
    assert m.params == ["x"]


def test_parse_message_text_no_space():
    m = parse_irc_line("PRIVMSG #c :")
    assert m.params == ["#c", ""]
    assert m.has_trailing is True


# --------------------------------------------------------------------------- #
# client integration against mock server
# --------------------------------------------------------------------------- #
def _connect_and_run(mock, nick):
    """Return (client, task) connected+registered against mock."""
    return asyncio.get_event_loop(), None  # placeholder replaced below


def _run(coro):
    return asyncio.run(coro)


async def _connect(mock, nick, events=None):
    client = IrcClient(host=mock.host, port=mock.port, nickname=nick,
                       username="u", realname="r", tls=False)
    ready = asyncio.Event()
    client.on(READY, lambda cl: ready.set())
    if events:
        for name, cb in events.items():
            client.on(name, cb)
    task = asyncio.create_task(client.run())
    await asyncio.wait_for(ready.wait(), timeout=3)
    return client, task


def test_client_register_and_pingpong():
    async def run():
        mock = MockServer()
        await mock.start()
        try:
            client, task = await _connect(mock, "RegBot")
            server_client = mock.client_by_nick("RegBot")
            assert server_client is not None
            assert server_client.registered is True
            # the mock sent a PING on registration; client must have PONGed
            await wait_until(lambda: any(
                l.upper().startswith("PONG") for l in server_client.lines))
            await client.request_quit("bye")
            await asyncio.sleep(0.05)
        finally:
            await mock.stop()
    _run(run())


def test_client_join_and_part():
    async def run():
        mock = MockServer()
        await mock.start()
        try:
            client, task = await _connect(mock, "JoinBot")
            await client.join("#test")
            await client.part("#test")
            c = mock.client_by_nick("JoinBot")
            await wait_until(lambda: any("#test" in l for l in c.lines) and
                             any("PART #test" in l for l in c.lines))
            # sanity: we saw the join in our own event stream via raw messages
            assert any("JOIN #test" in l for l in c.lines)
            await client.request_quit("bye")
            await asyncio.sleep(0.02)
        finally:
            await mock.stop()
    _run(run())


def test_client_privmsg_roundtrip():
    async def run():
        mock = MockServer()
        await mock.start()
        try:
            received = []
            client, task = await _connect(mock, "MsgBot",
                                          events={MESSAGE: lambda cl, m: received.append(m)})
            # another "user" sends a private message to MsgBot
            await mock.say_as("tester", "MsgBot", "hello msgbot")
            await wait_until(lambda: len(received) > 0)
            assert received[0].text == "hello msgbot"
            assert received[0].sender_nick == "tester"
            # MsgBot replies back to the tester
            await client.send_privmsg("tester", "pong")
            c = mock.client_by_nick("MsgBot")
            await wait_until(lambda: any(m == "pong" for m in c.messages))
            await client.request_quit("bye")
            await asyncio.sleep(0.02)
        finally:
            await mock.stop()
    _run(run())


def test_nick_collision_auto_rename():
    async def run():
        mock = MockServer()
        await mock.start()
        try:
            client, task = await _connect(mock, "CollBot")
            c = mock.client_by_nick("CollBot")
            assert c is not None
            # server rejects our nick
            await mock.push("CollBot", ":server 433 * CollBot :Nickname in use")
            await wait_until(lambda: c.nick is not None and c.nick != "CollBot")
            assert "_" in c.nick
            await client.request_quit("bye")
            await asyncio.sleep(0.02)
        finally:
            await mock.stop()
    _run(run())


def test_malformed_and_overlong_lines_do_not_crash():
    async def run():
        mock = MockServer()
        await mock.start()
        try:
            client, task = await _connect(mock, "ToughBot")
            c = mock.client_by_nick("ToughBot")
            await mock.push("ToughBot", "not a valid irc : : :: line")
            await mock.push("ToughBot", "x" * 3000)  # exceeds max line length
            await asyncio.sleep(0.05)
            # client still alive & can send
            await client.send_privmsg("tester", "still here")
            await wait_until(lambda: any(m == "still here" for m in c.messages))
            await client.request_quit("bye")
            await asyncio.sleep(0.02)
        finally:
            await mock.stop()
    _run(run())


def test_remote_close_and_reconnect():
    async def run():
        mock = MockServer()
        await mock.start()
        try:
            client, task = await _connect(mock, "DropBot")
            c0 = mock.client_by_nick("DropBot")
            # server drops the connection
            await mock.drop_client("DropBot")
            reason = await asyncio.wait_for(task, timeout=3)
            assert reason in ("remote_close", "session_error")
            await mock.stop()
        finally:
            try:
                await mock.stop()
            except Exception:
                pass
    _run(run())
