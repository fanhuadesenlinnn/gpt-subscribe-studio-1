"""Local HTTP CONNECT -> upstream SOCKS5 bridge (with username/password).

Chrome's `--proxy-server` speaks HTTP proxy auth (`page.authenticate`) but has no
SOCKS5 username/password support, so a residential SOCKS5 gateway has to be
bridged through a local HTTP proxy for the hCaptcha solver to share the payment
attempt's egress IP.

Usage:
    SOCKS_UPSTREAM='socks5h://user:pass@host:3010' BRIDGE_PORT=18080 \
        python scripts/socks_bridge.py
"""
from __future__ import annotations

import asyncio
import os
from urllib.parse import unquote, urlsplit

UPSTREAM = os.environ["SOCKS_UPSTREAM"]
HOST = os.environ.get("BRIDGE_HOST", "127.0.0.1")
PORT = int(os.environ.get("BRIDGE_PORT", "18080"))


async def _socks_connect(host: str, port: int):
    parts = urlsplit(UPSTREAM)
    reader, writer = await asyncio.open_connection(parts.hostname, parts.port or 1080)
    writer.write(b"\x05\x01\x02")
    await writer.drain()
    greeting = await reader.readexactly(2)
    if greeting[1] == 0x02:
        user = unquote(parts.username or "").encode()
        password = unquote(parts.password or "").encode()
        writer.write(b"\x01" + bytes([len(user)]) + user + bytes([len(password)]) + password)
        await writer.drain()
        status = await reader.readexactly(2)
        if status[1] != 0:
            raise ConnectionError("socks auth rejected")
    raw = host.encode()
    writer.write(b"\x05\x01\x00\x03" + bytes([len(raw)]) + raw + int(port).to_bytes(2, "big"))
    await writer.drain()
    reply = await reader.readexactly(4)
    if reply[1] != 0:
        raise ConnectionError(f"socks connect failed: {reply[1]}")
    atyp = reply[3]
    if atyp == 1:
        await reader.readexactly(4)
    elif atyp == 3:
        size = (await reader.readexactly(1))[0]
        await reader.readexactly(size)
    elif atyp == 4:
        await reader.readexactly(16)
    await reader.readexactly(2)
    return reader, writer


async def _pipe(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
    try:
        while True:
            chunk = await src.read(65536)
            if not chunk:
                break
            dst.write(chunk)
            await dst.drain()
    except Exception:  # noqa: BLE001
        pass
    finally:
        try:
            dst.close()
        except Exception:  # noqa: BLE001
            pass


async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        request_line = await reader.readline()
        if not request_line:
            writer.close()
            return
        method, target, _ = request_line.decode("latin-1").split()
        while True:
            line = await reader.readline()
            if line in (b"\r\n", b"\n", b""):
                break
        if method.upper() != "CONNECT":
            writer.write(b"HTTP/1.1 405 Method Not Allowed\r\nConnection: close\r\n\r\n")
            await writer.drain()
            writer.close()
            return
        host, _, port = target.rpartition(":")
        up_reader, up_writer = await _socks_connect(host, int(port))
        writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await writer.drain()
        await asyncio.gather(_pipe(reader, up_writer), _pipe(up_reader, writer))
    except Exception:  # noqa: BLE001
        try:
            writer.write(b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n")
            await writer.drain()
        except Exception:  # noqa: BLE001
            pass
        try:
            writer.close()
        except Exception:  # noqa: BLE001
            pass


async def main() -> None:
    server = await asyncio.start_server(_handle, HOST, PORT)
    print(f"socks bridge on http://{HOST}:{PORT} -> {urlsplit(UPSTREAM).hostname}", flush=True)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
