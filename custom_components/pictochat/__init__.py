"""PictoChat: a Nintendo DS as a remote control and message display for Home Assistant.

The integration connects to a PictoChat host (pictochat.py in this repository), which holds a chat room open for the
DS through an ESP32-C3 radio. Messages from the DS are matched against learned phrases; a match fires an event and
runs the phrase's action. Home Assistant can send text and pictures back into the room.
"""
from __future__ import annotations

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv, device_registry as dr

from .const import DOMAIN, PLATFORMS
from .hub import PictoChatHub

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

SEND_TEXT_SCHEMA = vol.Schema({
    vol.Required("message"): cv.string,
    vol.Optional("color", default=1): vol.All(vol.Coerce(int), vol.Range(min=1, max=15)),
})
SEND_IMAGE_SCHEMA = vol.Schema({
    vol.Required("path"): cv.string,
    vol.Optional("dither", default=True): cv.boolean,
    vol.Optional("color", default=False): cv.boolean,
})


def _hubs(hass: HomeAssistant) -> list[PictoChatHub]:
    hubs = [e.runtime_data for e in hass.config_entries.async_loaded_entries(DOMAIN)]
    if not hubs:
        raise HomeAssistantError("No PictoChat host is set up")
    return hubs


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    async def send_text(call: ServiceCall) -> None:
        for hub in _hubs(hass):
            await hub.send_text(call.data["message"], call.data["color"])

    async def send_image(call: ServiceCall) -> None:
        for hub in _hubs(hass):
            await hub.send_image(call.data["path"], call.data["dither"], call.data["color"])

    hass.services.async_register(DOMAIN, "send_text", send_text, SEND_TEXT_SCHEMA)
    hass.services.async_register(DOMAIN, "send_image", send_image, SEND_IMAGE_SCHEMA)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    hub = PictoChatHub(hass, entry)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, entry.entry_id)}, name=entry.title,
        manufacturer="pictochat-host", model="PictoChat room")
    hub.device_id = device.id
    # the last picture survives a reload, so a phrase can be learned from it right after the options changed
    kept = hass.data.setdefault(DOMAIN, {}).pop(entry.entry_id, None)
    if kept:
        hub.last_pixels, hub.last_time, hub.last_sender = kept
    entry.runtime_data = hub
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_create_background_task(hass, hub.client.run(), "pictochat host connection")
    entry.async_on_unload(entry.add_update_listener(_reload))
    return True


async def _reload(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    hub: PictoChatHub = entry.runtime_data
    if hub.last_pixels is not None:
        hass.data.setdefault(DOMAIN, {})[entry.entry_id] = (hub.last_pixels, hub.last_time, hub.last_sender)
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
