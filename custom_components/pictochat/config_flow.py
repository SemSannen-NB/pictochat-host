"""Setup of a PictoChat host, and the options flow that manages the phrases."""
from __future__ import annotations

import uuid

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, OptionsFlow
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.util import dt as dt_util

from . import picture
from .client import CannotConnect, InvalidAuth, probe
from .const import (ACTIONS, CONF_TOKEN, DEFAULT_PORT, DEFAULT_THRESHOLD, DOMAIN, OPT_PHRASES, OPT_THRESHOLD,
                    OPT_UNKNOWN_REPLY, TARGET_DOMAINS)

PASSWORD = selector.TextSelector(selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD))


class PictoChatConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input=None):
        errors = {}
        if user_input is not None:
            host, port = user_input[CONF_HOST].strip(), int(user_input[CONF_PORT])
            token = user_input.get(CONF_TOKEN, "").strip()
            await self.async_set_unique_id("%s:%d" % (host, port))
            self._abort_if_unique_id_configured()
            try:
                await probe(host, port, token)
            except InvalidAuth:
                errors["base"] = "invalid_auth"
            except CannotConnect:
                errors["base"] = "cannot_connect"
            else:
                return self.async_create_entry(title="PictoChat",
                                               data={CONF_HOST: host, CONF_PORT: port, CONF_TOKEN: token})
        schema = vol.Schema({
            vol.Required(CONF_HOST, default="host.docker.internal"): str,
            vol.Required(CONF_PORT, default=DEFAULT_PORT): vol.All(vol.Coerce(int), vol.Range(min=1, max=65535)),
            vol.Optional(CONF_TOKEN): PASSWORD,
        })
        return self.async_show_form(step_id="user", data_schema=self.add_suggested_values_to_schema(
            schema, user_input or {}), errors=errors)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return PictoChatOptionsFlow()


def _phrase_schema(phrase: dict) -> vol.Schema:
    return vol.Schema({
        vol.Required("name", default=phrase.get("name", vol.UNDEFINED)): selector.TextSelector(),
        vol.Optional("targets", default=phrase.get("targets", [])): selector.EntitySelector(
            selector.EntitySelectorConfig(domain=TARGET_DOMAINS, multiple=True)),
        vol.Required("action", default=phrase.get("action", "toggle")): selector.SelectSelector(
            selector.SelectSelectorConfig(options=ACTIONS, translation_key="action",
                                          mode=selector.SelectSelectorMode.DROPDOWN)),
        vol.Optional("reply", description={"suggested_value": phrase.get("reply", "")}): selector.TextSelector(),
    })


class PictoChatOptionsFlow(OptionsFlow):
    def __init__(self) -> None:
        self._editing: str | None = None          # id of the phrase being edited

    @property
    def _phrases(self) -> list[dict]:
        return [dict(p) for p in self.config_entry.options.get(OPT_PHRASES, [])]

    def _save(self, **changes):
        return self.async_create_entry(title="", data={**self.config_entry.options, **changes})

    async def async_step_init(self, user_input=None):
        menu = ["add_phrase", "edit_phrase", "remove_phrase", "settings"]
        if not self._phrases:
            menu = ["add_phrase", "settings"]
        return self.async_show_menu(step_id="init", menu_options=menu)

    # ---- learn a phrase from the last picture a DS sent
    async def async_step_add_phrase(self, user_input=None):
        hub = getattr(self.config_entry, "runtime_data", None)
        if hub is None or hub.last_pixels is None:
            return self.async_abort(reason="no_message")
        sig = picture.Signature.from_pixels(hub.last_pixels)
        if sig is None:
            return self.async_abort(reason="empty_message")
        errors = {}
        placeholders = {"sender": hub.last_sender or "DS",
                        "ago": str(max(0, round((dt_util.utcnow() - hub.last_time).total_seconds()))),
                        "match": ""}
        phrases = self._phrases
        if user_input is not None:
            name = user_input["name"].strip()
            known, score = picture.best_match(sig, phrases)
            same = next((p for p in phrases if p["name"].lower() == name.lower()), None)
            if not name:
                errors["name"] = "name_required"
            elif known is not None and score >= hub.threshold and known is not same:
                errors["base"] = "already_matches"            # the same picture cannot mean two phrases
                placeholders["match"] = known["name"]
            elif same is not None:
                # a known name gets one more sample: another spelling, or one more try at a drawing
                if not (known is same and score == 1.0):
                    same["samples"].append(sig.dumps())
                return self._save(**{OPT_PHRASES: phrases})
            else:
                phrases.append({"id": uuid.uuid4().hex[:8], "name": name, "samples": [sig.dumps()],
                                "targets": user_input.get("targets", []), "action": user_input["action"],
                                "reply": user_input.get("reply", "").strip()})
                return self._save(**{OPT_PHRASES: phrases})
        return self.async_show_form(
            step_id="add_phrase", errors=errors, description_placeholders=placeholders,
            data_schema=self.add_suggested_values_to_schema(_phrase_schema({}), user_input or {}))

    # ---- edit
    async def async_step_edit_phrase(self, user_input=None):
        if user_input is not None:
            self._editing = user_input["phrase"]
            return await self.async_step_edit_details()
        options = [selector.SelectOptionDict(value=p["id"], label=p["name"]) for p in self._phrases]
        return self.async_show_form(step_id="edit_phrase", data_schema=vol.Schema({
            vol.Required("phrase"): selector.SelectSelector(selector.SelectSelectorConfig(options=options))}))

    async def async_step_edit_details(self, user_input=None):
        phrases = self._phrases
        phrase = next((p for p in phrases if p["id"] == self._editing), None)
        if phrase is None:
            return self.async_abort(reason="unknown_phrase")
        errors = {}
        if user_input is not None:
            name = user_input["name"].strip()
            if not name:
                errors["name"] = "name_required"
            elif any(p is not phrase and p["name"].lower() == name.lower() for p in phrases):
                errors["name"] = "name_exists"
            else:
                phrase.update(name=name, targets=user_input.get("targets", []), action=user_input["action"],
                              reply=user_input.get("reply", "").strip())
                return self._save(**{OPT_PHRASES: phrases})
        return self.async_show_form(
            step_id="edit_details", errors=errors, data_schema=_phrase_schema(phrase),
            description_placeholders={"name": phrase["name"], "samples": str(len(phrase["samples"]))})

    # ---- remove
    async def async_step_remove_phrase(self, user_input=None):
        if user_input is not None:
            gone = set(user_input.get("phrases", []))
            return self._save(**{OPT_PHRASES: [p for p in self._phrases if p["id"] not in gone]})
        options = [selector.SelectOptionDict(value=p["id"], label=p["name"]) for p in self._phrases]
        return self.async_show_form(step_id="remove_phrase", data_schema=vol.Schema({
            vol.Optional("phrases", default=[]): selector.SelectSelector(
                selector.SelectSelectorConfig(options=options, multiple=True))}))

    # ---- settings
    async def async_step_settings(self, user_input=None):
        if user_input is not None:
            return self._save(**{OPT_THRESHOLD: int(user_input[OPT_THRESHOLD]),
                                 OPT_UNKNOWN_REPLY: user_input.get(OPT_UNKNOWN_REPLY, "").strip()})
        opts = self.config_entry.options
        return self.async_show_form(step_id="settings", data_schema=vol.Schema({
            vol.Required(OPT_THRESHOLD, default=opts.get(OPT_THRESHOLD, DEFAULT_THRESHOLD)): selector.NumberSelector(
                selector.NumberSelectorConfig(min=50, max=100, step=1, unit_of_measurement="%",
                                              mode=selector.NumberSelectorMode.SLIDER)),
            vol.Optional(OPT_UNKNOWN_REPLY, description={"suggested_value": opts.get(OPT_UNKNOWN_REPLY, "")}):
                selector.TextSelector(),
        }))
