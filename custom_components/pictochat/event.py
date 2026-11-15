"""Event entity: fires for every picture a DS sends."""
from __future__ import annotations

from homeassistant.core import callback
from homeassistant.components.event import EventEntity
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from .entity import PictoChatEntity
from .hub import signal_message


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([MessageEvent(entry.runtime_data, "message")])


class MessageEvent(PictoChatEntity, EventEntity):
    _attr_event_types = ["phrase", "unknown"]

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(async_dispatcher_connect(
            self.hass, signal_message(self.hub.entry.entry_id), self._message))

    @callback
    def _message(self, data: dict) -> None:
        self._trigger_event("phrase" if data["recognized"] else "unknown",
                            {k: data[k] for k in ("phrase", "sender", "score", "best_guess")})
        self.async_write_ha_state()
