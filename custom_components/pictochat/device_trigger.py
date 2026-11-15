"""Device triggers: one per learned phrase, plus one for a message that matched nothing."""
from __future__ import annotations

import voluptuous as vol

from homeassistant.components.device_automation import DEVICE_TRIGGER_BASE_SCHEMA
from homeassistant.components.homeassistant.triggers import event as event_trigger
from homeassistant.const import CONF_DEVICE_ID, CONF_DOMAIN, CONF_PLATFORM, CONF_TYPE
from homeassistant.core import CALLBACK_TYPE, HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.trigger import TriggerActionType, TriggerInfo
from homeassistant.helpers.typing import ConfigType

from .const import DOMAIN, EVENT_MESSAGE, EVENT_PHRASE, OPT_PHRASES

CONF_SUBTYPE = "subtype"
TYPE_PHRASE, TYPE_UNKNOWN = "phrase", "unknown"

TRIGGER_SCHEMA = DEVICE_TRIGGER_BASE_SCHEMA.extend({
    vol.Required(CONF_TYPE): vol.In([TYPE_PHRASE, TYPE_UNKNOWN]),
    vol.Optional(CONF_SUBTYPE): str,
})


async def async_get_triggers(hass: HomeAssistant, device_id: str) -> list[dict]:
    device = dr.async_get(hass).async_get(device_id)
    base = {CONF_PLATFORM: "device", CONF_DOMAIN: DOMAIN, CONF_DEVICE_ID: device_id}
    # config_entry_id is the current attribute; Home Assistant before 2026 has primary_config_entry
    entry_id = device and (getattr(device, "config_entry_id", None) or device.primary_config_entry)
    entry = hass.config_entries.async_get_entry(entry_id) if entry_id else None
    phrases = entry.options.get(OPT_PHRASES, []) if entry and entry.domain == DOMAIN else []
    return ([{**base, CONF_TYPE: TYPE_PHRASE, CONF_SUBTYPE: p["name"]} for p in phrases]
            + [{**base, CONF_TYPE: TYPE_UNKNOWN}])


async def async_attach_trigger(hass: HomeAssistant, config: ConfigType, action: TriggerActionType,
                               trigger_info: TriggerInfo) -> CALLBACK_TYPE:
    if config[CONF_TYPE] == TYPE_PHRASE:
        event_type = EVENT_PHRASE
        data = {"device_id": config[CONF_DEVICE_ID], "phrase": config.get(CONF_SUBTYPE)}
    else:
        event_type = EVENT_MESSAGE
        data = {"device_id": config[CONF_DEVICE_ID], "recognized": False}
    event_config = event_trigger.TRIGGER_SCHEMA({
        event_trigger.CONF_PLATFORM: "event",
        event_trigger.CONF_EVENT_TYPE: event_type,
        event_trigger.CONF_EVENT_DATA: data,
    })
    return await event_trigger.async_attach_trigger(hass, event_config, action, trigger_info,
                                                    platform_type="device")
