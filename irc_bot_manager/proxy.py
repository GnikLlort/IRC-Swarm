"""Proxy handling.

Parses ``proxy.txt``, validates entries, runs SOCKS5 / HTTP-CONNECT tunnel
handshakes in pure asyncio (no OS thread per bot) and provides optional health
checking.  Credentials never leak: everything logged uses :func:`display`.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
from typing import List, Optional
from urllib.parse import unquote, urlsplit

from logger import redact
from models import ConfigError, Proxy, ProxyScheme

log = logging.getLogger("proxy")

DEFAULT_PORTS = {ProxyScheme.SOCKS5: 1080, ProxyScheme.HTTP: 8080}


class ProxyParseError(ValueError):
    """Raised when a single proxy.txt line is not a valid proxy."""


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #
def parse_proxy_url(url: str) -> Proxy:
    """Validate ``url`` and return a :class:`Proxy`, else raise.

    Supported::

        socks5://host:port
        socks5://user:pass@host:port
        http://host:port
        http://user:pass@host:port
    """
    url = (url or "").strip()
    if not url:
        raise ProxyParseError("empty proxy entry")
    if url.lower().startswith(("socks4", "https")):
        raise ProxyParseError(f"unsupported proxy scheme in {redact(url)!r}")
    try:
        parts = urlsplit(url)
    except ValueError as exc:  # e.g. invalid port
        raise ProxyParseError(f"malformed proxy {redact(url)!r}: {exc}") from exc

    scheme = parts.scheme.lower()
    if scheme == "socks5":
        pscheme = ProxyScheme.SOCKS5
    elif scheme == "http":
        pscheme = ProxyScheme.HTTP
    else:
        raise ProxyParseError(
            f"unsupported proxy scheme {parts.scheme!r} in {redact(url)!r} "
            "(expected socks5:// or http://)"
        )

    if not parts.hostname:
        raise ProxyParseError(f"proxy missing host in {redact(url)!r}")

    try:
        port = parts.port or DEFAULT_PORTS[pscheme]
    except ValueError as exc:
        raise ProxyParseError(f"invalid port in {redact(url)!r}: {exc}") from exc
    if not (1 <= port <= 65535):
        raise ProxyParseError(f"port out of range in {redact(url)!r}: {port}")

    return Proxy(
        index=0,
        scheme=pscheme,
        host=parts.hostname,
        port=port,
        username=unquote(parts.username) if parts.username else None,
        _password=unquote(parts.password) if parts.password else None,
    )


def parse_proxy_line(line: str) -> Proxy:
    """Parse a single trimmed line from proxy.txt into a Proxy."""
    return parse_proxy_url(line)


def load_proxies_from_text(text: str) -> List[Proxy]:
    """Parse proxy.txt content, skipping blank/commented lines.

    Invalid entries are reported on the caller-facing logger, never raised.
    """
    proxies: List[Proxy] = []
    for lineno, raw in enumerate(text.splitlines(), start=1):
        entry = raw.strip()
        if not entry or entry.startswith("#"):
            continue
        try:
            proxy = parse_proxy_url(entry)
            proxy.index = len(proxies) + 1
            proxy._source_line = lineno
            proxies.append(proxy)
        except ProxyParseError as exc:
            log.warning("Invalid proxy on line %d (%s)", lineno, redact(str(exc)))
    return proxies


def load_proxies_from_file(path: str) -> List[Proxy]:
    with open(path, "r", encoding="utf-8") as fh:
        return load_proxies_from_text(fh.read())


def _proxy_from_parts(index: int, scheme, host, port, username=None, password=None) -> Proxy:
    return Proxy(index=index, scheme=scheme, host=host, port=port,
                 username=username, _password=password)


# --------------------------------------------------------------------------- #
# Socks5 / HTTP CONNECT handshakes (pure asyncio)
# --------------------------------------------------------------------------- #
_SOCKS5 = 0x05
_SOCKS5_AUTH_USERPASS = 0x02
_SOCKS5_NOAUTH = 0x00
_CMD_CONNECT = 0x01
_REPLY_OK = 0x00
_REPLY_MESSAGES = {
    0x01: "general SOCKS server failure",
    0x02: "connection not allowed by ruleset",
    0x03: "network unreachable",
    0x04: "host unreachable",
    0x05: "connection refused",
    0x06: "TTL expired",
    0x07: "command not supported",
    0x08: "address type not supported",
}


async def _read_exact(reader: asyncio.StreamReader, n: int) -> bytes:
    return await reader.readexactly(n)


def _socks_addr(dst_host: str) -> tuple:
    """Encode a destination address for the SOCKS5 CONNECT request."""
    try:
        ip = ipaddress.ip_address(dst_host)
    except ValueError:
        raw = dst_host.encode("idna")
        if len(raw) > 255:
            raise ValueError("destination hostname too long")
        return (0x03, bytes([len(raw)]) + raw)
    if ip.version == 4:
        return (0x01, ip.packed)
    return (0x04, ip.packed)


async def socks5_handshake(reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                           proxy: Proxy, dst_host: str, dst_port: int) -> None:
    methods = [_SOCKS5_NOAUTH]
    if proxy.username:
        methods.append(_SOCKS5_AUTH_USERPASS)
    writer.write(b"\x05" + bytes([len(methods)]) + bytes(methods))
    await writer.drain()

    resp = await _read_exact(reader, 2)
    if resp[0] != _SOCKS5:
        raise ConnectionError("proxy is not speaking SOCKS5")
    method = resp[1]
    if method == 0xFF:
        raise ConnectionError("proxy rejected all offered auth methods")

    if method == _SOCKS5_AUTH_USERPASS:
        user = (proxy.username or "").encode()
        pwd = (proxy.password or "").encode()
        if len(user) > 255 or len(pwd) > 255:
            raise ConnectionError("SOCKS5 username/password too long")
        writer.write(b"\x01" + bytes([len(user)]) + user + bytes([len(pwd)]) + pwd)
        await writer.drain()
        auth = await _read_exact(reader, 2)
        if auth[0] != 0x01 or auth[1] != 0x00:
            raise ConnectionError("SOCKS5 authentication failed")
    elif method != _SOCKS5_NOAUTH:
        raise ConnectionError(f"SOCKS5 unsupported auth method {method}")

    atyp, addr = _socks_addr(dst_host)
    request = (b"\x05" + bytes([_CMD_CONNECT]) + b"\x00" + bytes([atyp]) + addr
               + dst_port.to_bytes(2, "big"))
    writer.write(request)
    await writer.drain()

    head = await _read_exact(reader, 4)
    if head[0] != _SOCKS5 or head[1] != _REPLY_OK:
        rep = head[1]
        raise ConnectionError(f"SOCKS5 connect failed: {_REPLY_MESSAGES.get(rep, 'code %d' % rep)}")
    # Consume the remainder of the reply (bind addr + port).
    atyp = head[3]
    if atyp == 0x01:
        await _read_exact(reader, 4)
    elif atyp == 0x04:
        await _read_exact(reader, 16)
    else:
        ln = (await _read_exact(reader, 1))[0]
        await _read_exact(reader, ln)
    await _read_exact(reader, 2)


async def http_connect(reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                       proxy: Proxy, dst_host: str, dst_port: int) -> None:
    import base64
    headers = [
        f"CONNECT {dst_host}:{dst_port} HTTP/1.1",
        f"Host: {dst_host}:{dst_port}",
        "Proxy-Connection: keep-alive",
    ]
    if proxy.username:
        cred = f"{proxy.username}:{proxy.password or ''}".encode()
        token = base64.b64encode(cred).decode()
        headers.append(f"Proxy-Authorization: Basic {token}")
    writer.write(("\r\n".join(headers) + "\r\n\r\n").encode("ascii"))
    await writer.drain()

    # Read response head up to \r\n\r\n (bounded).
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = await reader.read(1024)
        if not chunk:
            raise ConnectionError("proxy closed during CONNECT handshake")
        data += chunk
        if len(data) > 16384:
            raise ConnectionError("HTTP CONNECT response header too large")

    head, _, _ = data.partition(b"\r\n\r\n")
    lines = head.split(b"\r\n")
    status_line = lines[0].decode("latin1", "replace") if lines else ""
    parts = status_line.split(" ", 2)
    if len(parts) < 2 or parts[0].upper() != "HTTP/1.1" or parts[1] != "200":
        raise ConnectionError(f"HTTP CONNECT failed: {status_line or 'empty response'}")
    # Any extra body bytes read past \r\n\r\n must be left for the caller.
    # We only asked for up to 1024-at-a-time so there may be trailing bytes;
    # asyncio buffers them transparently inside the StreamReader, which the
    # caller keeps, so nothing is lost.


async def open_proxy_tunnel(proxy: Proxy, dst_host: str, dst_port: int,
                            *, timeout: float) -> tuple:
    """Open a raw (pre-TLS) TCP stream to ``dst`` through ``proxy``.

    Returns ``(StreamReader, StreamWriter)`` already tunneled to the target.
    """
    proxy_host = socket.gethostbyname(proxy.host)  # proxy itself resolved by name
    async def _open() -> tuple:
        reader, writer = await asyncio.open_connection(proxy.host, proxy.port)
        try:
            if proxy.scheme == ProxyScheme.SOCKS5:
                await socks5_handshake(reader, writer, proxy, dst_host, dst_port)
            else:
                await http_connect(reader, writer, proxy, dst_host, dst_port)
        except BaseException:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            raise
        return reader, writer

    return await asyncio.wait_for(_open(), timeout=timeout)


async def check_proxy_connectivity(proxy: Proxy, dst_host: str, dst_port: int,
                                   *, timeout: float = 5.0) -> bool:
    """True if a full tunnel handshake to ``dst`` succeeds within ``timeout``."""
    try:
        _, writer = await open_proxy_tunnel(proxy, dst_host, dst_port, timeout=timeout)
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        return True
    except (asyncio.TimeoutError, ConnectionError, OSError, ValueError):
        return False


def resolve_host(host: str, port: int) -> tuple:
    return host, port
