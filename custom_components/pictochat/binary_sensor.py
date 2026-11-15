"""Binary sensor: is a DS in the room?"""
from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity

from .entity import PictoChatEntity


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([DsInRoom(entry.runtime_data, "ds_in_room")])


class DsInRoom(PictoChatEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    @property
    def is_on(self) -> bool:
        return bool(self.hub.present)

    @property
    def extra_state_attributes(self) -> dict:
        return {"count": len(self.hub.present),
                "names": [self.hub.names.get(m, "DS") for m in self.hub.present]}
