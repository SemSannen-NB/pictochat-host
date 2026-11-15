"""Notify entity: sends a text into the PictoChat room."""
from __future__ import annotations

from homeassistant.components.notify import NotifyEntity

from .entity import PictoChatEntity


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([RoomNotify(entry.runtime_data, "room")])


class RoomNotify(PictoChatEntity, NotifyEntity):
    async def async_send_message(self, message: str, title: str | None = None) -> None:
        await self.hub.send_text("%s\n%s" % (title, message) if title else message)
