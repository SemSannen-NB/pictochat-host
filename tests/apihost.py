"""Test helper: the real host's API port (pictochat.GuiBridge) without a radio.

usage: apihost.py PORT TOKEN. Every JSON line on stdin is sent to the API clients as an event; every command a client
sends is printed as a JSON line on stdout."""
import json
import os
import queue
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import pictochat  # noqa: E402

t0 = time.time()
cmdq = queue.Queue()
bridge = pictochat.GuiBridge(int(sys.argv[1]), cmdq, "127.0.0.1", sys.argv[2], lambda: time.time() - t0)


def commands():
    while True:
        print(json.dumps(cmdq.get()), flush=True)


threading.Thread(target=commands, daemon=True).start()
print(json.dumps({"listening": int(sys.argv[1])}), flush=True)
for line in sys.stdin:
    ev = json.loads(line)
    ev["t"] = time.time() - t0
    bridge.send(ev)
