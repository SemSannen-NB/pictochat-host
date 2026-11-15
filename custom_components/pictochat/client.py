"""Connection to the PictoChat host's JSON-lines API (API.md in the repository)."""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable

_LOGGER = logging.getLogger(__name__)
LINE_LIMIT = 1 << 20            # a full picture is about 21 kB of hex on one line


class CannotConnect(Exception):
    """The host did not answer."""


class InvalidAuth(Exception):
    """The host refused the token."""


async def _open(host: str, port: int, token: str):
    reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port, limit=LINE_LIMIT), 10)
    if token:
        writer.write((json.dumps({"cmd": "auth", "token": token}) + "\n").encode())
        await writer.drain()
    return reader, writer


async def probe(host: str, port: int, token: str) -> None:
    """Checks that a host answers on host:port and accepts the token. Raises CannotConnect or InvalidAuth."""
    try:
        reader, writer = await _open(host, port, token)
    except (OSError, asyncio.TimeoutError) as e:
        raise CannotConnect(str(e)) from e
    try:
        # a host answers at once with its replay and "ready"; an older one within 5 s with a "state" event
        line = await asyncio.wait_for(reader.readline(), 8)
        ev = json.loads(line) if line else None
    except (OSError, asyncio.TimeoutError, ValueError) as e:
        raise CannotConnect(str(e)) from e
    finally:
        writer.close()
    if not isinstance(ev, dict) or "kind" not in ev:
        raise CannotConnect("not a PictoChat host")
    if ev["kind"] == "error":
        raise InvalidAuth(ev.get("text", ""))


class PictoChatClient:
    """Keeps a connection to the host open, reconnects when it drops and hands every event to `on_event`."""

    def __init__(self, host: str, port: int, token: str, on_event: Callable[[dict], None],
                 on_connection: Callable[[bool], None]):
        self.host, self.port, self.token = host, port, token
        self._on_event, self._on_connection = on_event, on_connection
        self._writer: asyncio.StreamWriter | None = None

    @property
    def connected(self) -> bool:
        return self._writer is not None

    async def run(self) -> None:
        delay = 1
        while True:
            try:
                reader, writer = await _open(self.host, self.port, self.token)
            except (OSError, asyncio.TimeoutError) as e:
                _LOGGER.debug("PictoChat host %s:%d not reachable: %s", self.host, self.port, e)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30)
                continue
            self._writer = writer
            self._on_connection(True)
            try:
                while line := await reader.readline():
                    delay = 1
                    try:
                        ev = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(ev, dict):
                        if ev.get("kind") == "error":
                            _LOGGER.error("PictoChat host %s:%d: %s", self.host, self.port, ev.get("text"))
                            delay = 30
                            break
                        self._on_event(ev)
            except (OSError, ValueError, asyncio.IncompleteReadError) as e:      # ValueError: line over the limit
                _LOGGER.debug("PictoChat host connection lost: %s", e)
            finally:
                self._writer = None
                writer.close()
                self._on_connection(False)
            await asyncio.sleep(delay)

    async def send(self, obj: dict) -> None:
        if self._writer is None:
            raise ConnectionError("not connected to the PictoChat host")
        self._writer.write((json.dumps(obj, separators=(",", ":")) + "\n").encode())
        await self._writer.drain()
