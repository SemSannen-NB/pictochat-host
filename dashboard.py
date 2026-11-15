#!/usr/bin/env python3
"""PictoChat dashboard: a web page in PictoChat style to test the room, and the one program a deployment runs.

  python3 dashboard.py            then open http://localhost:8080

It starts the host (pictochat.py) on the ESP32-C3 it finds, restarts it when it stops, shows the room's messages,
sends your drawings and texts, and can flash the ESP32-C3 with the modem firmware in firmware/.
Only the standard library is used here; the host needs pyserial and flashing needs esptool.

Settings (environment variables):
  DASHBOARD_PORT       web port (8080)
  DASHBOARD_PASSWORD   if set, the page asks for it (any user name)
  PICTOCHAT_SERIAL     the ESP32-C3's serial port; empty = the first /dev/ttyACM*, /dev/ttyUSB* or /dev/cu.usbmodem*
  PICTOCHAT_CHANNEL    Wi-Fi channel (1)        PICTOCHAT_ROOM   0-3 = Chat Room A-D (0)
  PICTOCHAT_NAME       nickname on the DS (Home)
  PICTOCHAT_TOKEN      if set, the host's API on port 7777 is opened to other machines with this token
  DASHBOARD_NO_HOST    if set, do not start a host: only connect to one that already runs on 127.0.0.1:7777
"""
import base64
import glob
import hmac
import json
import os
import queue
import signal
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
FIRMWARE = os.path.join(HERE, "firmware")
ENV = os.environ.get
WEB_PORT = int(ENV("DASHBOARD_PORT") or 8080)
PASSWORD = ENV("DASHBOARD_PASSWORD") or ""
TOKEN = ENV("PICTOCHAT_TOKEN") or ""
API_PORT = 7777
NAME = (ENV("PICTOCHAT_NAME") or "Home")[:10]
ROOM = int(ENV("PICTOCHAT_ROOM") or 0)


class Feed:
    """Events for the browsers: the recent ones are kept for a page that opens later."""

    def __init__(self):
        self.lock = threading.Lock()
        self.subscribers = []
        self.recent = []                  # messages and log lines, replayed to a new page
        self.sticky = {}                  # kind -> the latest event of that kind (status, state)

    def publish(self, ev, keep=False, sticky=False):
        with self.lock:
            if sticky:
                self.sticky[ev["kind"]] = ev
            if keep:
                self.recent.append(ev)
                del self.recent[:-400]
            for q in list(self.subscribers):
                try:
                    q.put_nowait(ev)
                except queue.Full:        # a page that stopped reading
                    self.subscribers.remove(q)

    def reset(self):
        """The host (re)connected and replays its own history: start the page's history again."""
        with self.lock:
            self.recent.clear()
            self.sticky.pop("state", None)
        self.publish({"kind": "reset"})

    def subscribe(self):
        q = queue.Queue(maxsize=2000)
        with self.lock:
            for ev in list(self.sticky.values()) + self.recent:
                q.put_nowait(ev)
            self.subscribers.append(q)
        return q

    def unsubscribe(self, q):
        with self.lock:
            if q in self.subscribers:
                self.subscribers.remove(q)


feed = Feed()


def note(text):
    """A line of the dashboard itself in the page's log."""
    print(text, flush=True)
    feed.publish({"kind": "log", "text": text, "own": True}, keep=True)


def find_port():
    port = ENV("PICTOCHAT_SERIAL")
    if port:
        return port if os.path.exists(port) else None
    found = sorted(glob.glob("/dev/ttyACM*") + glob.glob("/dev/ttyUSB*") + glob.glob("/dev/cu.usbmodem*"))
    return found[0] if found else None


# ---------------------------------------------------------------- the host process
class Supervisor:
    def __init__(self):
        self.proc = None
        self.lock = threading.Lock()      # held while flashing: the host must stay away from the serial port
        self.status("starting")

    def status(self, host, port=None, detail=""):
        ev = {"kind": "status", "host": host, "port": port, "detail": detail, "name": NAME, "room": ROOM,
              "password": bool(PASSWORD), "firmware": os.path.exists(os.path.join(FIRMWARE, "flash_args"))}
        if feed.sticky.get("status") != ev:                       # not again every few seconds while waiting
            feed.publish(ev, sticky=True)

    def run(self):
        while True:
            with self.lock:
                port = find_port()
                if port:
                    cmd = [sys.executable, os.path.join(HERE, "pictochat.py"), "host", "--serial", port, "--offload",
                           "--channel", ENV("PICTOCHAT_CHANNEL") or "1", "--room", str(ROOM), "--name", NAME,
                           "--gui-port", str(API_PORT), "--out", "none"]
                    if TOKEN:
                        cmd += ["--gui-bind", "0.0.0.0"]          # the token itself is in the environment
                    self.proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
                    self.status("running", port)
                    note("# dashboard: host started on %s" % port)
            if not port:
                self.status("no_radio", detail="No ESP32-C3 found. Plug it in.")
                time.sleep(3)
                continue
            last = ""
            for line in self.proc.stderr:                         # until the host exits
                if line.strip():
                    last = line.strip()
                    note("# host: " + last)
            code = self.proc.wait()
            self.proc = None
            self.status("stopped", port, last or "The host stopped (exit code %d)." % code)
            time.sleep(5)

    def stop(self):
        p = self.proc
        if p and p.poll() is None:
            p.terminate()
            try:
                p.wait(5)
            except subprocess.TimeoutExpired:
                p.kill()

    def flash(self):
        """Stops the host, writes firmware/ to the ESP32-C3 with esptool and lets the host start again."""
        if not self.lock.acquire(blocking=False):
            return

        def say(text, **kw):
            feed.publish({"kind": "flash", "text": text, **kw})

        try:
            self.stop()
            port = find_port()
            if not port:
                return say("No ESP32-C3 found. Plug it in and try again.", done=True, ok=False)
            self.status("flashing", port)
            say("Flashing %s ...\n" % port)
            cmd = [sys.executable, "-m", "esptool", "--chip", "esp32c3", "--port", port, "--baud", "460800",
                   "write-flash", "@flash_args"]
            try:
                p = subprocess.Popen(cmd, cwd=FIRMWARE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            except OSError as e:
                return say("Cannot start esptool: %s" % e, done=True, ok=False)
            buf = b""
            while True:
                c = p.stdout.read(1)
                if c in (b"\r", b"\n", b""):                      # progress lines end in \r
                    if buf.strip():
                        say(buf.decode("utf-8", "replace") + "\n", progress=c == b"\r")
                    buf = b""
                    if c == b"":
                        break
                else:
                    buf += c
            ok = p.wait() == 0
            say("\nDone. The host starts again in a moment." if ok else
                "\nFlashing failed. If the board did not answer: hold BOOT, press RESET, release BOOT, and try again.",
                done=True, ok=ok)
            time.sleep(2)                                         # the board restarts into the new firmware
        finally:
            self.lock.release()


sup = Supervisor()


# ---------------------------------------------------------------- the link to the host's API
class HostLink:
    def __init__(self):
        self.sock = None
        self.wlock = threading.Lock()

    def run(self):
        while True:
            try:
                s = socket.create_connection(("127.0.0.1", API_PORT), timeout=3)
            except OSError:
                time.sleep(1)
                continue
            s.settimeout(None)
            if TOKEN:
                s.sendall((json.dumps({"cmd": "auth", "token": TOKEN}) + "\n").encode())
            self.sock = s
            feed.reset()
            buf = b""
            try:
                while True:
                    d = s.recv(65536)
                    if not d:
                        break
                    buf += d
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        try:
                            self.handle(json.loads(line))
                        except ValueError:
                            pass
            except OSError:
                pass
            self.sock = None
            s.close()
            feed.publish({"kind": "state", "offline": True}, sticky=True)
            time.sleep(1)

    def handle(self, ev):
        kind = ev.get("kind")
        if kind == "message":
            feed.publish(ev, keep=True)
        elif kind == "state":
            feed.publish(ev, sticky=True)
        elif kind == "log" and ev.get("text", "").startswith("#") and not ev["text"].startswith("# status"):
            feed.publish(ev, keep=True)

    def send(self, obj):
        s = self.sock
        if s is None:
            raise OSError("the host is not running")
        with self.wlock:
            s.sendall((json.dumps(obj, separators=(",", ":")) + "\n").encode())


link = HostLink()


# ---------------------------------------------------------------- the web server
class Web(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def allowed(self):
        if not PASSWORD:
            return True
        try:
            kind, data = self.headers.get("Authorization", "").split(" ", 1)
            given = base64.b64decode(data).decode().split(":", 1)[1]
        except (ValueError, IndexError, UnicodeDecodeError):
            given = None
        if given is not None and hmac.compare_digest(given.encode(), PASSWORD.encode()):
            return True
        if given is not None:
            time.sleep(1)                                         # slows down guessing
        self.reply(401, b"password required", extra={"WWW-Authenticate": 'Basic realm="PictoChat"'})
        return False

    def reply(self, code, body, ctype="text/plain; charset=utf-8", extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self' 'unsafe-inline'; frame-ancestors 'none'")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self.allowed():
            return
        if self.path == "/":
            with open(os.path.join(HERE, "dashboard.html"), "rb") as fh:
                self.reply(200, fh.read(), "text/html; charset=utf-8")
        elif self.path == "/events":
            self.events()
        else:
            self.reply(404, b"not found")

    def events(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")               # a proxy must not hold the stream back
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        q = feed.subscribe()
        try:
            while True:
                try:
                    data = "data: %s\n\n" % json.dumps(q.get(timeout=15), separators=(",", ":"))
                except queue.Empty:
                    data = ": still here\n\n"
                self.wfile.write(data.encode())
                self.wfile.flush()
        except OSError:
            pass
        finally:
            feed.unsubscribe(q)

    def do_POST(self):
        if not self.allowed():
            return
        # a page of another site must not be able to send or flash through the visitor's browser
        if self.headers.get("X-Dashboard") != "1":
            return self.reply(403, b"forbidden")
        try:
            size = int(self.headers.get("Content-Length") or 0)
            if not 0 <= size <= 1 << 20:                          # a full picture is about 45 kB of JSON
                self.close_connection = True
                return self.reply(413, b"too large")
            body = json.loads(self.rfile.read(size) or b"{}")
        except ValueError:
            return self.reply(400, b"bad request")
        if not isinstance(body, dict):
            return self.reply(400, b"bad request")
        if self.path == "/api/send":
            px = body.get("pixels")
            if not (isinstance(px, list) and len(px) == 80 and all(
                    isinstance(r, list) and len(r) == 256 and all(type(v) is int and 0 <= v <= 15 for v in r)
                    for r in px)):
                return self.reply(400, b"pixels must be 80 rows of 256 palette indices")
            try:
                link.send({"cmd": "chat", "pixels": px})
            except OSError as e:
                return self.reply(503, str(e).encode())
            self.reply(200, b"ok")
        elif self.path == "/api/flash":
            if not os.path.exists(os.path.join(FIRMWARE, "flash_args")):
                return self.reply(404, b"no firmware in firmware/: run firmware/build.sh")
            threading.Thread(target=sup.flash, daemon=True).start()
            self.reply(200, b"ok")
        elif self.path == "/api/restart":
            sup.stop()
            self.reply(200, b"ok")
        else:
            self.reply(404, b"not found")


def main():
    if not ENV("DASHBOARD_NO_HOST"):
        threading.Thread(target=sup.run, daemon=True).start()
    else:
        sup.status("external")
    threading.Thread(target=link.run, daemon=True).start()
    signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))        # docker stop: also stop the host
    srv = ThreadingHTTPServer(("0.0.0.0", WEB_PORT), Web)
    srv.daemon_threads = True
    print("PictoChat dashboard on http://localhost:%d%s" % (WEB_PORT, "" if PASSWORD else "  (no password set)"),
          flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        sup.stop()


if __name__ == "__main__":
    main()
