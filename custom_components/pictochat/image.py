"""Image: the last picture a DS sent."""
from __future__ import annotations

from homeassistant.core import callback
from homeassistant.components.image import ImageEntity

from . import picture
from .entity import PictoChatEntity


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([LastMessage(hass, entry.runtime_data)])


class LastMessage(PictoChatEntity, ImageEntity):
    _attr_content_type = "image/png"

    def __init__(self, hass, hub):
        PictoChatEntity.__init__(self, hub, "last_message")
        ImageEntity.__init__(self, hass)
        self._shown = None
        self._png: bytes | None = None
        self._attr_image_last_updated = hub.last_time

    @property
    def available(self) -> bool:
        return True                       # the last picture stays viewable while the host is away

    @callback
    def _updated(self) -> None:
        self._attr_image_last_updated = self.hub.last_time
        self.async_write_ha_state()

    async def async_image(self) -> bytes | None:
        pixels = self.hub.last_pixels
        if pixels is None:
            return None
        if self._shown is not pixels:
            self._png = await self.hass.async_add_executor_job(picture.to_png, pixels)
            self._shown = pixels
        return self._png
