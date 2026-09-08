"""Tests for proxy.txt parsing, redaction and tunnel handshakes."""

import asyncio
import logging

import pytest

from logger import redact, register_secret
from models import ProxyScheme
import proxy as proxy_mod
from proxy import (check_proxy_connectivity, load_proxies_from_text,
                   parse_proxy_url, ProxyParseError)


# --------------------------------------------------------------------------- #
# parsing
# --------------------------------------------------------------------------- #
def test_parse_socks5():
    p = parse_proxy_url("socks5://127.0.0.1:9050")
    assert p.scheme == ProxyScheme.SOCKS5
    assert p.host == "127.0.0.1"
    assert p.port == 9050
    assert p.username is None


def test_parse_socks5_with_creds():
    p = parse_proxy_url("socks5://user:secret@10.0.0.10:1080")
    assert p.username == "user"
    assert p.password == "secret"
    assert p.display() == "socks5://10.0.0.10:1080"
    assert "secret" not in p.display()
    assert "secret" not in str(p)
    assert "secret" not in repr(p)


def test_parse_http_default_port():
    p = parse_proxy_url("http://192.168.1.20")
    assert p.scheme == ProxyScheme.HTTP
    assert p.port == 8080


def test_parse_http_with_creds_redaction():
    p = parse_proxy_url("http://user:pw@1.2.3.4:8080")
    assert p.username == "user"
    assert p.password == "pw"
    assert "pw" not in p.display()


def test_invalid_scheme():
    with pytest.raises(ProxyParseError):
        parse_proxy_url("socks4://1.2.3.4:1080")


def test_missing_host():
    with pytest.raises(ProxyParseError):
        parse_proxy_url("socks5://:1080")


def test_bad_port():
    with pytest.raises(ProxyParseError):
        parse_proxy_url("socks5://1.2.3.4:notaport")


def test_empty_rejected():
    with pytest.raises(ProxyParseError):
        parse_proxy_url("   ")


def test_loading_skips_comments_and_blanks():
    text = (
        "# header comment\n"
        "\n"
        "socks5://127.0.0.1:9050\n"
        "   # indented comment   \n"
        "  http://1.2.3.4:8080  \n"
        "\n"
    )
    proxies = load_proxies_from_text(text)
    assert len(proxies) == 2
    assert proxies[0].scheme == ProxyScheme.SOCKS5
    assert proxies[1].scheme == ProxyScheme.HTTP
    assert proxies[0].index == 1
    assert proxies[1].index == 2


def test_invalid_line_does_not_crash(caplog):
    text = "socks5://127.0.0.1:9050\ninvalid line\nhttp://x:8080\n"
    with caplog.at_level(logging.WARNING):
        proxies = load_proxies_from_text(text)
    assert len(proxies) == 2
    assert any("line 2" in r.message for r in caplog.records)


def test_redact_url_password():
    assert redact("connecting to socks5://u:hunter2@host:1080") == \
        "connecting to socks5://u:****@host:1080"


def test_redact_registered_secret():
    secret = "s3cret_" + "topsecretpw"
    register_secret(secret)
    assert secret not in redact(f"auth secret is {secret}")


def test_scheme_normalisation():
    p = parse_proxy_url("SOCKS5://127.0.0.1:9050")
    assert p.scheme == ProxyScheme.SOCKS5


# --------------------------------------------------------------------------- #
# handshakes
# --------------------------------------------------------------------------- #
async def _fake_socks5_server():
    """A minimal scripted SOCKS5 proxy that accepts the CONNECT handshake."""
    got = {}

    async def handler(reader, writer):
        ver, nmethods = await reader.readexactly(2)
        await reader.readexactly(nmethods)
        writer.write(b"\x05\x00")
        await writer.drain()
        head = await reader.readexactly(4)
        atyp = head[3]
        if atyp == 0x01:
            await reader.readexactly(4)
        elif atyp == 0x03:
            ln = (await reader.readexactly(1))[0]
            await reader.readexactly(ln)
        elif atyp == 0x04:
            await reader.readexactly(16)
        await reader.readexactly(2)
        writer.write(b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00")
        await writer.drain()
        got["handshake"] = True
        writer.close()
        await writer.wait_closed()

    srv = await asyncio.start_server(handler, "127.0.0.1", 0)
    port = srv.sockets[0].getsockname()[1]
    return srv, port, got


async def _fake_http_server():
    got = {}

    async def handler(reader, writer):
        data = b""
        while b"\r\n\r\n" not in data:
            data += await reader.read(1024)
        writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
        await writer.drain()
        got["handshake"] = True
        writer.close()
        await writer.wait_closed()

    srv = await asyncio.start_server(handler, "127.0.0.1", 0)
    port = srv.sockets[0].getsockname()[1]
    return srv, port, got


def _proxy(port, scheme="socks5"):
    url = f"{scheme}://127.0.0.1:{port}"
    p = parse_proxy_url(url)
    return p


def test_socks5_handshake_success():
    async def run():
        srv, port, got = await _fake_socks5_server()
        try:
            ok = await check_proxy_connectivity(
                _proxy(port, "socks5"), "203.0.113.1", 6667, timeout=2)
            assert ok is True
            assert got.get("handshake") is True
        finally:
            srv.close()
            await srv.wait_closed()
    asyncio.run(run())


def test_socks5_with_credentials_handshake():
    async def run():
        calls = []

        async def handler(reader, writer):
            ver, nmethods = await reader.readexactly(2)
            methods = await reader.readexactly(nmethods)
            if b"\x02" in methods:
                writer.write(b"\x05\x02")
                await writer.drain()
                await reader.readexactly(1)          # auth version 0x01
                ulen = (await reader.readexactly(1))[0]
                await reader.readexactly(ulen)       # username
                plen = (await reader.readexactly(1))[0]
                await reader.readexactly(plen)       # password
                calls.append("auth")
                writer.write(b"\x01\x00")
                await writer.drain()
            else:
                writer.write(b"\x05\x00")
                await writer.drain()
            head = await reader.readexactly(4)
            atyp = head[3]
            if atyp == 0x01:
                await reader.readexactly(4)
            else:
                ln = (await reader.readexactly(1))[0]
                await reader.readexactly(ln)
            await reader.readexactly(2)
            writer.write(b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00")
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        srv = await asyncio.start_server(handler, "127.0.0.1", 0)
        port = srv.sockets[0].getsockname()[1]
        try:
            p = parse_proxy_url(f"socks5://bob:hunter2@127.0.0.1:{port}")
            ok = await check_proxy_connectivity(p, "203.0.113.1", 6667, timeout=2)
            assert ok is True
            assert "auth" in calls
        finally:
            srv.close()
            await srv.wait_closed()
    asyncio.run(run())


def test_http_connect_handshake_success():
    async def run():
        srv, port, got = await _fake_http_server()
        try:
            ok = await check_proxy_connectivity(
                _proxy(port, "http"), "198.51.100.5", 6667, timeout=2)
            assert ok is True
            assert got.get("handshake") is True
        finally:
            srv.close()
            await srv.wait_closed()
    asyncio.run(run())


def test_unreachable_proxy_fails():
    async def run():
        p = parse_proxy_url("socks5://127.0.0.1:1")  # nothing listening there
        ok = await check_proxy_connectivity(p, "203.0.113.1", 6667, timeout=1)
        assert ok is False
    asyncio.run(run())


def test_http_bad_status_fails():
    async def handler(reader, writer):
        data = b""
        while b"\r\n\r\n" not in data:
            data += await reader.read(1024)
        writer.write(b"HTTP/1.1 407 Proxy Authentication Required\r\n\r\n")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    async def run():
        srv = await asyncio.start_server(handler, "127.0.0.1", 0)
        port = srv.sockets[0].getsockname()[1]
        try:
            ok = await check_proxy_connectivity(
                _proxy(port, "http"), "x", 6667, timeout=2)
            assert ok is False
        finally:
            srv.close()
            await srv.wait_closed()
    asyncio.run(run())
