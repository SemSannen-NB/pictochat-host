"""The hub: one connection to a PictoChat host, the room's state and the phrase recognition."""
from __future__ import annotations

import logging
from datetime import datetime

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_ENTITY_ID, CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.util import dt as dt_util

from . import picture
from .client import PictoChatClient
from .const import (CONF_TOKEN, DEFAULT_THRESHOLD, DOMAIN, EVENT_MESSAGE, EVENT_PHRASE, OPT_PHRASES, OPT_THRESHOLD,
                    OPT_UNKNOWN_REPLY)

_LOGGER = logging.getLogger(__name__)


def signal_update(entry_id: str) -> str:
    """Dispatcher signal: the room's state changed."""
    return "%s_%s_update" % (DOMAIN, entry_id)


def signal_message(entry_id: str) -> str:
    """Dispatcher signal: a live picture from a DS was handled. Argument: the event data."""
    return "%s_%s_message" % (DOMAIN, entry_id)


class PictoChatHub:
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry):
        self.hass, self.entry = hass, entry
        self.client = PictoChatClient(entry.data[CONF_HOST], entry.data[CONF_PORT], entry.data.get(CONF_TOKEN, ""),
                                      self._on_event, self._on_connection)
        self.connected = False
        self.live = False                         # False while the host replays its history after a connect
        self.info: dict = {}                      # version, impl, radio of the host
        self.names: dict[str, str] = {}           # DS MAC -> nickname
        self.present: dict[str, int] = {}         # MAC -> AID of every DS in the room
        self.rssi: dict[str, float] = {}          # MAC -> smoothed dBm
        # the last picture a DS sent; the options flow learns phrases from it
        self.last_pixels: list[list[int]] | None = None
        self.last_time: datetime | None = None
        self.last_sender: str | None = None
        self.last_phrase: str | None = None       # name of the phrase the last picture matched, if any
        self.last_score: float = 0.0
        self.device_id: str | None = None

    # ---- options
    @property
    def phrases(self) -> list[dict]:
        return self.entry.options.get(OPT_PHRASES, [])

    @property
    def threshold(self) -> float:
        return self.entry.options.get(OPT_THRESHOLD, DEFAULT_THRESHOLD) / 100

    # ---- connection
    @callback
    def _on_connection(self, up: bool) -> None:
        self.connected = up
        self.live = False
        if not up:
            self.present, self.rssi = {}, {}
        async_dispatcher_send(self.hass, signal_update(self.entry.entry_id))

    @callback
    def _on_event(self, ev: dict) -> None:
        kind = ev.get("kind")
        if kind == "ready":
            self.live = True
            self.info["version"] = ev.get("version")
        elif kind == "state":
            self.live = True                      # a host before 0.3.0 has no "ready": its first state ends the replay
            self.info = {k: ev.get(k) for k in ("version", "impl", "radio", "commit")}
            self.present = dict(ev.get("aids") or {})
            self.rssi = {m: v[1] for m, v in (ev.get("rssi") or {}).items() if m in self.present}
            self._update_device()
            async_dispatcher_send(self.hass, signal_update(self.entry.entry_id))
        elif kind == "message" and ev.get("dir") == "rx":
            try:
                msg = bytes.fromhex(ev.get("hex", ""))
            except ValueError:
                return
            if picture.is_member(msg):
                self.names[picture.sender_mac(msg)] = picture.member_name(msg)
                async_dispatcher_send(self.hass, signal_update(self.entry.entry_id))
            elif picture.is_chat(msg):
                self._on_picture(msg)

    def _update_device(self) -> None:
        if self.device_id and self.info.get("version"):
            dr.async_get(self.hass).async_update_device(
                self.device_id, sw_version=self.info["version"], model=self.info.get("radio"))

    # ---- pictures from a DS
    @callback
    def _on_picture(self, msg: bytes) -> None:
        pixels = picture.decode_image(msg)
        if pixels is None:
            return
        mac = picture.sender_mac(msg)
        sender = self.names.get(mac) or "DS"
        self.last_pixels, self.last_time, self.last_sender = pixels, dt_util.utcnow(), sender
        self.last_phrase, self.last_score = None, 0.0
        sig = picture.Signature.from_pixels(pixels)
        phrase, score = picture.best_match(sig, self.phrases) if sig else (None, 0.0)
        if phrase is not None and score >= self.threshold:
            self.last_phrase, self.last_score = phrase["name"], score
        async_dispatcher_send(self.hass, signal_update(self.entry.entry_id))
        if not self.live:
            return                                # history: keep it as the last picture, but never act on it
        data = {"entry_id": self.entry.entry_id, "device_id": self.device_id, "sender": sender, "sender_mac": mac,
                "recognized": self.last_phrase is not None, "phrase": self.last_phrase,
                "score": round(score, 3), "best_guess": phrase["name"] if phrase else None}
        self.hass.bus.async_fire(EVENT_MESSAGE, data)
        if self.last_phrase is not None:
            _LOGGER.debug("%s sent phrase %r (score %.3f)", sender, self.last_phrase, score)
            self.hass.bus.async_fire(EVENT_PHRASE, {**data, "phrase_id": phrase["id"]})
            self.hass.async_create_task(self._run_phrase(phrase))
        elif self.entry.options.get(OPT_UNKNOWN_REPLY):
            self.hass.async_create_task(self._reply(self.entry.options[OPT_UNKNOWN_REPLY]))
        async_dispatcher_send(self.hass, signal_message(self.entry.entry_id), data)

    async def _run_phrase(self, phrase: dict) -> None:
        action = phrase.get("action", "toggle")
        for entity_id in phrase.get("targets", []):
            domain = entity_id.split(".", 1)[0]
            if domain in ("button", "input_button"):
                service = "press"
            elif domain == "scene":
                service = "turn_on"
            else:
                service = action
            try:
                await self.hass.services.async_call(domain, service, {ATTR_ENTITY_ID: entity_id}, blocking=True)
            except Exception as e:                # noqa: BLE001  one broken target must not stop the others
                _LOGGER.warning("Phrase %r: %s.%s on %s failed: %s", phrase["name"], domain, service, entity_id, e)
        if phrase.get("reply"):
            await self._reply(phrase["reply"])

    async def _reply(self, text: str) -> None:
        try:
            await self.send_text(text)
        except HomeAssistantError as e:
            _LOGGER.warning("Could not answer the DS: %s", e)

    # ---- sending
    async def send_pixels(self, pixels: list[list[int]]) -> None:
        try:
            await self.client.send({"cmd": "chat", "pixels": pixels})
        except (ConnectionError, OSError) as e:
            raise HomeAssistantError("PictoChat host is not connected: %s" % e) from e

    async def send_text(self, text: str, color: int = 1) -> None:
        try:
            pictures = await self.hass.async_add_executor_job(picture.text_to_pictures, text, color)
        except ImportError as e:
            raise HomeAssistantError("Rendering text needs Pillow: %s" % e) from e
        for p in pictures:
            await self.send_pixels(p)

    async def send_image(self, path: str, dither: bool = True, color: bool = False) -> None:
        if not self.hass.config.is_allowed_path(path):
            raise HomeAssistantError("%s is not in allowlist_external_dirs" % path)
        try:
            pixels = await self.hass.async_add_executor_job(picture.image_to_picture, path, dither, color)
        except (OSError, ImportError, ValueError) as e:
            raise HomeAssistantError("Cannot read image %s: %s" % (path, e)) from e
        await self.send_pixels(pixels)
