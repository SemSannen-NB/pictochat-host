#!/usr/bin/env python3
"""Minimal PictoChat bot: answers every picture from a DS with a text, then sends the picture back.

usage: python3 examples/echo_bot.py [GUI_PORT]      (host started with --gui-port, default 7777)
API reference: API.md. Needs Pillow for text rendering (pip install pillow).
"""
import json
import os
import socket
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import pictochat_gui as gui     # noqa: E402  (picture helpers: decode_image, text_to_pixels, clip)

port = int(sys.argv[1]) if len(sys.argv) > 1 else 7777
if gui.text_to_pixels("x") is None:
    sys.exit("text rendering needs Pillow: run this with the venv (.venv/bin/python examples/echo_bot.py)")
try:
    sock = socket.create_connection(("127.0.0.1", port))
except OSError as e:
    sys.exit("no host on 127.0.0.1:%d (%s): start it with --gui-port %d" % (port, e, port))
started = time.time()
names = {}                      # AID -> nickname, from the DSes' member messages


def send(obj):
    sock.sendall((json.dumps(obj) + "\n").encode())


if os.environ.get("PICTOCHAT_TOKEN"):               # host started with --gui-token: authenticate first
    send({"cmd": "auth", "token": os.environ["PICTOCHAT_TOKEN"]})
send({"cmd": "set_name", "name": "echo-bot"})
buf = b""
while True:
    data = sock.recv(65536)
    if not data:
        sys.exit("host closed the connection")
    buf += data
    while b"\n" in buf:
        line, buf = buf.split(b"\n", 1)
        ev = json.loads(line)
        if ev.get("kind") != "message" or ev.get("dir") != "rx":
            continue
        msg = bytes.fromhex(ev["hex"])
        if msg[:2] in (b"\x03\x00", b"\x03\x01"):                  # member message: remember the nickname
            names[ev["id"]] = msg[8:28].decode("utf-16-le", "replace").rstrip("\0")
            continue
        if time.time() - started < 2 or msg[:2] != b"\x03\x02":      # skip the replayed backlog and non-pictures
            continue
        rows = gui.decode_image(msg)
        if rows is None:
            continue
        who = names.get(ev["id"], "DS %d" % ev["id"])
        send({"cmd": "chat", "pixels": gui.text_to_pixels("Nice one, %s! Here it is again:" % who)})
        send({"cmd": "chat", "pixels": gui.clip(rows)})
