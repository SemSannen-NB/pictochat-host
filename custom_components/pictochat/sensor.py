"""Sensors: the last recognised phrase and the signal strength of the DS."""
from __future__ import annotations

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.const import SIGNAL_STRENGTH_DECIBELS_MILLIWATT, EntityCategory

from .entity import PictoChatEntity


async def async_setup_entry(hass, entry, async_add_entities):
    hub = entry.runtime_data
    async_add_entities([LastPhrase(hub, "last_phrase"), Signal(hub, "signal")])


class LastPhrase(PictoChatEntity, SensorEntity):
    """The phrase matched by the last picture; unknown when that picture matched nothing."""

    @property
    def native_value(self) -> str | None:
        return self.hub.last_phrase

    @property
    def extra_state_attributes(self) -> dict:
        return {"sender": self.hub.last_sender, "score": round(self.hub.last_score, 3),
                "received": self.hub.last_time.isoformat() if self.hub.last_time else None}


class Signal(PictoChatEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.SIGNAL_STRENGTH
    _attr_native_unit_of_measurement = SIGNAL_STRENGTH_DECIBELS_MILLIWATT
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def native_value(self) -> float | None:
        return max(self.hub.rssi.values()) if self.hub.rssi else None
