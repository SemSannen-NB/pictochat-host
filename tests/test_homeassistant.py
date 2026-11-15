"""The Home Assistant integration against the real host API (tests/apihost.py), without a radio or a DS.

Needs pytest-homeassistant-custom-component: pip install pytest-homeassistant-custom-component pillow
"""
import asyncio
import json
import os
import socket
import sys

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.components import device_automation                      # noqa: E402
from homeassistant.data_entry_flow import FlowResultType                    # noqa: E402
from homeassistant.setup import async_setup_component                       # noqa: E402
from pytest_homeassistant_custom_component.common import MockConfigEntry    # noqa: E402

import pictochat as host                                                    # noqa: E402
from custom_components.pictochat import picture                             # noqa: E402
from custom_components.pictochat.const import DOMAIN                        # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
MAC = bytes.fromhex("0009bf123456")
TOKEN = "s3cret"


@pytest.fixture(autouse=True)
def _setup(enable_custom_integrations, socket_enabled):
    yield


class ApiHost:
    """tests/apihost.py as a subprocess."""

    async def start(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        self.proc = await asyncio.create_subprocess_exec(
            sys.executable, os.path.join(HERE, "apihost.py"), str(self.port), TOKEN,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, limit=1 << 20)
        assert json.loads(await self.proc.stdout.readline()) == {"listening": self.port}
        return self

    async def event(self, **ev):
        self.proc.stdin.write((json.dumps(ev) + "\n").encode())
        await self.proc.stdin.drain()

    async def ds_sends(self, text):
        msg = host.chat_message(MAC, host.bitmap_to_image(picture.text_to_pictures(text)[0]))
        await self.event(kind="message", id=1, dir="rx", hex=msg.hex())

    async def command(self):
        return json.loads(await asyncio.wait_for(self.proc.stdout.readline(), 5))

    async def stop(self):
        if self.proc.returncode is None:
            self.proc.kill()
        await self.proc.wait()


@pytest.fixture
async def api():
    h = await ApiHost().start()
    yield h
    await h.stop()


async def until(cond, what):
    for _ in range(100):
        if cond():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("timed out waiting for " + what)


async def test_config_flow(hass, api):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"host": "127.0.0.1", "port": api.port, "token": "wrong"})
    assert result["errors"] == {"base": "invalid_auth"}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"host": "127.0.0.1", "port": 1, "token": TOKEN})
    assert result["errors"] == {"base": "cannot_connect"}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"host": "127.0.0.1", "port": api.port, "token": TOKEN})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {"host": "127.0.0.1", "port": api.port, "token": TOKEN}
    await hass.async_block_till_done()
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    await until(lambda: entry.runtime_data.connected, "the connection")
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_phrase_runs_action(hass, api):
    assert await async_setup_component(hass, "input_boolean", {"input_boolean": {"lamp": {}, "tv": {}, "fan": {}}})
    entry = MockConfigEntry(domain=DOMAIN, title="PictoChat",
                            data={"host": "127.0.0.1", "port": api.port, "token": TOKEN})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    hub = lambda: entry.runtime_data                                   # noqa: E731  (a reload makes a new hub)
    await until(lambda: hub().live, "the end of the replay")
    phrase_events, message_events = [], []
    hass.bus.async_listen("pictochat_phrase", phrase_events.append)
    hass.bus.async_listen("pictochat_message", message_events.append)

    # nothing learned yet: the options flow has no message to learn from
    flow = await hass.config_entries.options.async_init(entry.entry_id)
    assert flow["type"] is FlowResultType.MENU and flow["menu_options"] == ["add_phrase", "settings"]
    flow = await hass.config_entries.options.async_configure(flow["flow_id"], {"next_step_id": "add_phrase"})
    assert flow["type"] is FlowResultType.ABORT and flow["reason"] == "no_message"

    # a DS joins and sends a message nobody knows
    await api.event(kind="message", id=1, dir="rx", hex=host.member_info_message(1, MAC, "Sem").hex())
    await api.event(kind="state", version="0.3.0", impl="python", radio="test", aids={"00:09:bf:12:34:56": 1},
                    rssi={"00:09:bf:12:34:56": [-40, -41.5, 0.1, 9]})
    await until(lambda: hass.states.get("binary_sensor.pictochat_ds_in_room").state == "on", "the DS")
    assert hass.states.get("binary_sensor.pictochat_ds_in_room").attributes["names"] == ["Sem"]
    assert hass.states.get("sensor.pictochat_ds_signal").state == "-41.5"
    await api.ds_sends("lights on")
    await until(lambda: message_events, "the message event")
    assert message_events[0].data["recognized"] is False and message_events[0].data["sender"] == "Sem"
    assert hass.states.get("event.pictochat_message").attributes["event_type"] == "unknown"
    png = await hass.data["image"].get_entity("image.pictochat_last_message").async_image()
    assert png[:4] == b"\x89PNG"

    # learn it: toggle the lamp and answer "OK"
    flow = await hass.config_entries.options.async_init(entry.entry_id)
    flow = await hass.config_entries.options.async_configure(flow["flow_id"], {"next_step_id": "add_phrase"})
    assert flow["type"] is FlowResultType.FORM and flow["description_placeholders"]["sender"] == "Sem"
    flow = await hass.config_entries.options.async_configure(flow["flow_id"], {
        "name": "lights on", "targets": ["input_boolean.lamp"], "action": "toggle", "reply": "OK"})
    assert flow["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert [p["name"] for p in entry.options["phrases"]] == ["lights on"]

    # the reload reconnects and the host replays the message: it must not run the action
    await until(lambda: hub().live, "the end of the replay after the reload")
    await asyncio.sleep(0.2)
    assert hass.states.get("input_boolean.lamp").state == "off" and not phrase_events
    assert hub().last_pixels is not None

    # the same picture cannot be learned as a second phrase
    flow = await hass.config_entries.options.async_init(entry.entry_id)
    flow = await hass.config_entries.options.async_configure(flow["flow_id"], {"next_step_id": "add_phrase"})
    flow = await hass.config_entries.options.async_configure(flow["flow_id"], {"name": "other", "action": "toggle"})
    assert flow["errors"] == {"base": "already_matches"}

    # the DS sends it again: lamp on, events, answer to the DS
    await api.ds_sends("lights on")
    await until(lambda: hass.states.get("input_boolean.lamp").state == "on", "the lamp")
    assert phrase_events[0].data["phrase"] == "lights on" and phrase_events[0].data["score"] == 1.0
    assert hass.states.get("sensor.pictochat_last_phrase").state == "lights on"
    assert hass.states.get("event.pictochat_message").attributes["event_type"] == "phrase"
    reply = await api.command()
    assert reply["cmd"] == "chat" and len(reply["pixels"]) == 80 and any(any(r) for r in reply["pixels"])

    # a different text does nothing
    await api.ds_sends("lights off")
    await until(lambda: len(message_events) == 3, "the third message")
    assert hass.states.get("input_boolean.lamp").state == "on" and len(phrase_events) == 1
    assert hass.states.get("sensor.pictochat_last_phrase").state == "unknown"

    # device triggers: one per phrase and one for unknown messages
    triggers = await device_automation.async_get_device_automations(
        hass, device_automation.DeviceAutomationType.TRIGGER, [hub().device_id])
    assert [(t["type"], t.get("subtype")) for t in triggers[hub().device_id] if t["domain"] == DOMAIN] == [
        ("phrase", "lights on"), ("unknown", None)]
    assert await async_setup_component(hass, "automation", {"automation": [
        {"alias": "by phrase", "action": {"service": "input_boolean.turn_on", "entity_id": "input_boolean.tv"},
         "trigger": {"platform": "device", "domain": DOMAIN, "device_id": hub().device_id, "type": "phrase",
                     "subtype": "lights on"}},
        {"alias": "by unknown", "action": {"service": "input_boolean.turn_on", "entity_id": "input_boolean.fan"},
         "trigger": {"platform": "device", "domain": DOMAIN, "device_id": hub().device_id, "type": "unknown"}}]})
    await api.ds_sends("lights on")
    await until(lambda: hass.states.get("input_boolean.tv").state == "on", "the automation")
    assert hass.states.get("input_boolean.lamp").state == "off"        # toggled back by the phrase's own action
    assert hass.states.get("input_boolean.fan").state == "off"
    assert (await api.command())["cmd"] == "chat"                      # the "OK" answer
    await api.ds_sends("something else")
    await until(lambda: hass.states.get("input_boolean.fan").state == "on", "the unknown-message automation")

    # Home Assistant talks to the DS
    await hass.services.async_call("notify", "send_message", {
        "entity_id": "notify.pictochat_room", "message": "Dinner is ready"}, blocking=True)
    assert (await api.command())["cmd"] == "chat"
    await hass.services.async_call(DOMAIN, "send_text", {"message": "hello", "color": 5}, blocking=True)
    sent = await api.command()
    assert {v for r in sent["pixels"] for v in r} == {0, 5}

    # the host goes away: entities become unavailable
    await api.stop()
    await until(lambda: hass.states.get("binary_sensor.pictochat_ds_in_room").state == "unavailable", "the loss")
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_edit_remove_and_settings(hass):
    """The options flow without a host: the entry loads and keeps trying to connect."""
    sample = picture.Signature.from_pixels(picture.text_to_pictures("tv")[0]).dumps()
    entry = MockConfigEntry(domain=DOMAIN, title="PictoChat", data={"host": "127.0.0.1", "port": 1, "token": ""},
                            options={"phrases": [{"id": "a1", "name": "tv", "samples": [sample], "targets": [],
                                                  "action": "toggle", "reply": ""},
                                                 {"id": "b2", "name": "fan", "samples": [], "targets": [],
                                                  "action": "toggle", "reply": ""}]})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get("binary_sensor.pictochat_ds_in_room").state == "unavailable"

    async def step(*inputs):
        flow = await hass.config_entries.options.async_init(entry.entry_id)
        for user_input in inputs:
            flow = await hass.config_entries.options.async_configure(flow["flow_id"], user_input)
        await hass.async_block_till_done()
        return flow

    flow = await step({"next_step_id": "edit_phrase"}, {"phrase": "a1"},
                      {"name": "fan", "action": "turn_on"})
    assert flow["errors"] == {"name": "name_exists"}
    flow = await step({"next_step_id": "edit_phrase"}, {"phrase": "a1"},
                      {"name": "telly", "targets": ["scene.movie"], "action": "turn_off", "reply": "bye"})
    assert flow["type"] is FlowResultType.CREATE_ENTRY
    first = entry.options["phrases"][0]
    assert (first["name"], first["targets"], first["action"], first["reply"], first["samples"]) == (
        "telly", ["scene.movie"], "turn_off", "bye", [sample])

    await step({"next_step_id": "remove_phrase"}, {"phrases": ["b2"]})
    assert [p["id"] for p in entry.options["phrases"]] == ["a1"]

    await step({"next_step_id": "settings"}, {"threshold": 80, "unknown_reply": "?"})
    assert entry.options["threshold"] == 80 and entry.options["unknown_reply"] == "?"
    assert entry.runtime_data.threshold == 0.8 and len(entry.options["phrases"]) == 1
    assert await hass.config_entries.async_unload(entry.entry_id)
