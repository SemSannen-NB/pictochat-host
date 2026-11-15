"""Base class of the PictoChat entities."""
from __future__ import annotations

from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity

from .const import DOMAIN
from .hub import PictoChatHub, signal_update


class PictoChatEntity(Entity):
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, hub: PictoChatHub, key: str):
        self.hub = hub
        self._attr_translation_key = key
        self._attr_unique_id = "%s_%s" % (hub.entry.entry_id, key)
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, hub.entry.entry_id)})

    @property
    def available(self) -> bool:
        return self.hub.connected

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(async_dispatcher_connect(
            self.hass, signal_update(self.hub.entry.entry_id), self._updated))

    @callback
    def _updated(self) -> None:
        self.async_write_ha_state()
