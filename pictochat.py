#!/usr/bin/env python3
"""
pictochat.py - a PictoChat room host for a real Nintendo DS, plus capture/decode tools.

Acts as the host (parent) of a PictoChat room using a monitor-mode Wi-Fi adapter with frame injection (developed with
the Nintendo Wi-Fi USB Connector, Ralink RT2570 / rt2500usb). A DS lists the room, joins it, and messages typed or
drawn in pictochat_gui.py are shown on the DS. No third-party dependencies (the GUI uses Pillow for text, optional).
The protocol is documented in PROTOCOL.md.

  sudo python3 pictochat.py host --iface <wifi-interface> --channel 1 --gui-port 7777   # the room
  python3 pictochat_gui.py                                                         # window: send text / drawings
  sudo python3 pictochat.py capture --iface <wifi-interface> --out captures/room_a      # sniff a real room
  python3 pictochat.py analyze captures/room_a.pcap                                # decode a capture
  python3 pictochat.py selftest
  python3 pictochat.py host --bridge 7778 --lockstep --channel 1                   # against the emulator (emu/)

capture:
  * puts the interface in monitor mode (unless --no-setup)
  * hops channels 1-13 until it sees a DS beacon (Nintendo vendor element 00:09:BF)
    or any frame to a 03:09:BF:00:00:xx multiplay address, then stays on that channel
  * writes <out>.pcap (radiotap, open in Wireshark) and <out>.log (decoded text)
  * type a label + Enter at any time to drop a marker into the log
host:
  * sets the adapter MAC to --mac (so the hardware ACKs the DS), beacons the room, answers auth/assoc,
    polls the DS (CMD/ACK) and runs the PictoChat join and message protocol (class MultiplayHost)
  * writes <out>.log / <out>.pcap (default host_log); --gui-port serves pictochat_gui.py on 127.0.0.1
"""

import argparse
import hmac
import json
import os
import queue
import select
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
from collections import Counter, defaultdict

__version__ = "0.3.0"


def build_commit(script=__file__):
    """Short git commit of the code that is running: from the checkout the script lives in, or from a file
    `<script name>.commit` next to it (written when the code is copied to a machine without git). '' if unknown."""
    here = os.path.dirname(os.path.abspath(script))
    try:
        r = subprocess.run(["git", "-C", here, "rev-parse", "--short", "HEAD"], capture_output=True, text=True,
                           timeout=3)
        # only when this folder is the top of the checkout (not just somewhere below another repository)
        top = subprocess.run(["git", "-C", here, "rev-parse", "--show-toplevel"], capture_output=True, text=True,
                             timeout=3).stdout.strip()
        if r.returncode == 0 and top and os.path.realpath(top) == os.path.realpath(here):
            return r.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        with open(os.path.splitext(os.path.abspath(script))[0] + ".commit") as fh:
            return fh.read().split()[0][:12]
    except (OSError, IndexError):
        return ""

# ---------------------------------------------------------------------------
# constants

NINTENDO_OUI = b"\x00\x09\xbf"
MP_PREFIX = b"\x03\x09\xbf"
MP_CMD = bytes.fromhex("0309bf000000")    # host -> clients (poll / data)
MP_REPLY = bytes.fromhex("0309bf000010")  # client -> host
MP_ACK = bytes.fromhex("0309bf000003")    # host -> clients (ack)
MP_OUTSTANDING = 100                       # offload: packets queued on the ESP32 at most (it holds 128)
BROADCAST = b"\xff" * 6
# Nintendo OUIs seen on DS / Wii / USB Connector hardware (00:1b:7a was the test DS Lite)
NINTENDO_OUIS = {bytes.fromhex(x) for x in ("0009bf", "001656", "0017ab", "00191d", "001ae9", "001b7a", "001bea", "001cbe", "001df0", "001e35", "001f32", "001fc5", "002147", "0022aa", "0022d7", "002331", "0023cc", "002444", "0024f3", "00253f", "0025a0", "002709", "40d28a", "e84ece", "a4c0e1", "7cbb8a", "9cbacf", "d86bf7")}

MGMT_NAMES = {0: "assoc-req", 1: "assoc-resp", 2: "reassoc-req", 3: "reassoc-resp",
              4: "probe-req", 5: "probe-resp", 8: "beacon", 10: "disassoc",
              11: "auth", 12: "deauth", 13: "action"}
CTRL_NAMES = {8: "block-ack-req", 9: "block-ack", 10: "ps-poll", 11: "rts",
              12: "cts", 13: "ack", 14: "cf-end", 15: "cf-end+ack"}
DATA_SUBTYPES = {0: "data", 1: "data+cf-ack", 2: "data+cf-poll", 3: "data+cf-ack+cf-poll",
                 4: "null", 5: "cf-ack", 6: "cf-poll", 7: "cf-ack+cf-poll"}


def mac(b):
    return ":".join("%02x" % x for x in b)


# ---------------------------------------------------------------------------
# radiotap

# bit -> (alignment, size)
_RT_FIELDS = {
    0: (8, 8), 1: (1, 1), 2: (1, 1), 3: (2, 4), 4: (2, 2), 5: (1, 1), 6: (1, 1),
    7: (2, 2), 8: (2, 2), 9: (2, 2), 10: (1, 1), 11: (1, 1), 12: (1, 1), 13: (1, 1),
    14: (2, 2), 15: (2, 2), 16: (1, 1), 17: (1, 1), 18: (4, 8), 19: (1, 3),
    20: (4, 8), 21: (2, 12), 22: (8, 12),
}


def parse_radiotap(buf):
    """Return (info dict, header length). info keys: tsft, flags, rate, freq, signal, fcs."""
    if len(buf) < 8 or buf[0] != 0:
        raise ValueError("not radiotap")
    hlen = struct.unpack_from("<H", buf, 2)[0]
    present = struct.unpack_from("<I", buf, 4)[0]
    off = 8
    p = present
    while p & 0x80000000:  # extended present words
        p = struct.unpack_from("<I", buf, off)[0]
        off += 4
    info = {}
    for bit in range(0, 23):
        if not (present >> bit) & 1:
            continue
        align, size = _RT_FIELDS[bit]
        off = (off + align - 1) & ~(align - 1)
        if off + size > hlen:
            break
        if bit == 0:
            info["tsft"] = struct.unpack_from("<Q", buf, off)[0]
        elif bit == 1:
            info["flags"] = buf[off]
        elif bit == 2:
            info["rate"] = buf[off] * 0.5
        elif bit == 3:
            info["freq"] = struct.unpack_from("<H", buf, off)[0]
        elif bit == 5:
            info["signal"] = struct.unpack_from("<b", buf, off)[0]
        off += size
    info["fcs"] = bool(info.get("flags", 0) & 0x10)
    return info, hlen


# ---------------------------------------------------------------------------
# 802.11

class Frame:
    __slots__ = ("ts", "tsft", "rate", "freq", "signal", "raw", "ftype", "subtype",
                 "fc1", "dur", "a1", "a2", "a3", "seq", "frag", "body", "hdrlen", "cls")


def freq_to_ch(f):
    if f is None:
        return None
    if f == 2484:
        return 14
    if 2412 <= f <= 2472:
        return (f - 2407) // 5
    return None


def parse_frame(ts, pkt, linktype=127):
    """Decode one captured packet into a Frame (or None if too short/not 802.11)."""
    info = {}
    if linktype == 127:
        try:
            info, hl = parse_radiotap(pkt)
        except Exception:
            return None
        data = pkt[hl:]
    else:
        data = pkt
    if info.get("fcs") and len(data) >= 4:
        data = data[:-4]
    if len(data) < 10:
        return None
    f = Frame()
    f.ts = ts
    f.tsft = info.get("tsft")
    f.rate = info.get("rate")
    f.freq = info.get("freq")
    f.signal = info.get("signal")
    f.raw = data
    fc0, fc1 = data[0], data[1]
    f.ftype = (fc0 >> 2) & 3
    f.subtype = fc0 >> 4
    f.fc1 = fc1
    f.dur = struct.unpack_from("<H", data, 2)[0]
    f.a1 = data[4:10]
    f.a2 = f.a3 = None
    f.seq = f.frag = None
    f.body = b""
    f.hdrlen = 10
    if f.ftype != 1:  # management or data
        if len(data) < 24:
            return None
        f.a2 = data[10:16]
        f.a3 = data[16:22]
        sc = struct.unpack_from("<H", data, 22)[0]
        f.seq, f.frag = sc >> 4, sc & 0xF
        hl = 24
        if f.ftype == 2:
            if (fc1 & 3) == 3:
                hl += 6
            if f.subtype & 8:
                hl += 2
        f.hdrlen = hl
        f.body = data[hl:]
    elif len(data) >= 16:
        f.a2 = data[10:16]
        f.hdrlen = 16
    f.cls = classify(f)
    return f


def classify(f):
    if f.ftype == 0:
        return MGMT_NAMES.get(f.subtype, "mgmt-%d" % f.subtype)
    if f.ftype == 1:
        return "ctrl:" + CTRL_NAMES.get(f.subtype, str(f.subtype))
    if f.ftype == 2:
        if f.a1 == MP_CMD:
            return "MP-CMD"
        if f.a1 == MP_REPLY or f.a3 == MP_REPLY:   # real replies are ToDS: a1=BSSID, a2=client, a3=03:09:BF:00:00:10
            return "MP-REPLY"
        if f.a1 == MP_ACK:
            return "MP-ACK"
        return "data" if f.subtype == 0 else DATA_SUBTYPES.get(f.subtype & 7, "data?")
    return "reserved"


def parse_ies(b):
    ies = []
    i = 0
    while i + 2 <= len(b):
        tag, ln = b[i], b[i + 1]
        if i + 2 + ln > len(b):
            ies.append((tag, b[i + 2:], True))  # truncated
            break
        ies.append((tag, b[i + 2:i + 2 + ln], False))
        i += 2 + ln
    return ies


def describe_beacon(f):
    """Human-readable decode of a beacon/probe-response body."""
    b = f.body
    if len(b) < 12:
        return "short body"
    interval = struct.unpack_from("<H", b, 8)[0]
    cap = struct.unpack_from("<H", b, 10)[0]
    parts = ["interval=%dTU cap=0x%04x" % (interval, cap)]
    nin = None
    for tag, d, trunc in parse_ies(b[12:]):
        if tag == 0:
            parts.append("ssid=%r" % d.decode("latin1"))
        elif tag == 1:
            parts.append("rates=" + ",".join("%g%s" % ((x & 0x7F) / 2, "*" if x & 0x80 else "") for x in d))
        elif tag == 3 and d:
            parts.append("ch=%d" % d[0])
        elif tag == 5 and len(d) >= 3:
            parts.append("tim(count=%d period=%d ctl=%d bmp=%s)" % (d[0], d[1], d[2], d[3:].hex()))
        elif tag == 0xDD and d[:3] == NINTENDO_OUI:
            nin = d
            parts.append("NINTENDO-IE(len=%d type=%d)" % (len(d), d[3] if len(d) > 3 else -1))
        elif tag == 0xDD:
            parts.append("vendor(%s,%dB)" % (d[:3].hex(), len(d)))
        else:
            parts.append("ie%d(%s)" % (tag, d.hex()))
        if trunc:
            parts.append("[truncated]")
    s = " ".join(parts)
    if nin is not None and len(nin) >= 8:
        a, bb = struct.unpack_from("<HH", nin, 4)
        s += "\n        nintendo-ie: fieldA=0x%04x fieldB=0x%04x gameinfo[%d]=%s" % (
            a, bb, len(nin) - 8, nin[8:].hex())
    elif nin is not None:
        s += "\n        nintendo-ie raw=%s" % nin.hex()
    return s


def has_nintendo_ie(f):
    if f.ftype != 0 or f.subtype not in (5, 8) or len(f.body) < 12:
        return False
    return any(t == 0xDD and d[:3] == NINTENDO_OUI for t, d, _ in parse_ies(f.body[12:]))


def hexdump(b, indent="        ", width=16, limit=96):
    lines = []
    for i in range(0, min(len(b), limit), width):
        chunk = b[i:i + width]
        lines.append("%s%04x  %-*s" % (indent, i, width * 3, " ".join("%02x" % x for x in chunk)))
    if len(b) > limit:
        lines.append("%s... (%d more bytes)" % (indent, len(b) - limit))
    return "\n".join(lines)


def describe(f, t0):
    ch = freq_to_ch(f.freq)
    head = "%9.3f ch%-3s %4s Mb %4s dBm  %-12s" % (
        f.ts - t0, ch if ch else "?", ("%g" % f.rate) if f.rate else "?",
        f.signal if f.signal is not None else "?", f.cls)
    if f.ftype == 1:
        return head + " ra=%s%s" % (mac(f.a1), (" ta=" + mac(f.a2)) if f.a2 else "")
    fl = []
    if f.fc1 & 1:
        fl.append("toDS")
    if f.fc1 & 2:
        fl.append("fromDS")
    if f.fc1 & 8:
        fl.append("retry")
    line = head + " %s > %s bssid=%s seq=%d%s len=%d %s" % (
        mac(f.a2), mac(f.a1), mac(f.a3), f.seq, ("/f%d" % f.frag) if f.frag else "",
        len(f.raw), ",".join(fl))
    if f.cls in ("beacon", "probe-resp"):
        line += "\n        " + describe_beacon(f)
    elif f.ftype == 2 and f.body:
        line += "\n" + hexdump(f.body)
    elif f.ftype == 0 and f.body:
        line += "\n        body=" + f.body[:48].hex()
    return line


# ---------------------------------------------------------------------------
# pcap

def pcap_header(linktype=127):
    return struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, linktype)


def pcap_record(ts, pkt):
    sec = int(ts)
    return struct.pack("<IIII", sec, int((ts - sec) * 1e6), len(pkt), len(pkt)) + pkt


def read_pcap(path):
    """Read a pcap file (also gzip-compressed .pcap.gz). Yields (timestamp, packet, linktype)."""
    import gzip
    with (gzip.open(path, "rb") if str(path).endswith(".gz") else open(path, "rb")) as fh:
        gh = fh.read(24)
        if len(gh) < 24:
            raise ValueError("truncated pcap")
        magic = struct.unpack("<I", gh[:4])[0]
        if magic in (0xA1B2C3D4, 0xA1B23C4D):
            en = "<"
        elif struct.unpack(">I", gh[:4])[0] in (0xA1B2C3D4, 0xA1B23C4D):
            en = ">"
        else:
            raise ValueError("not a pcap file")
        nano = struct.unpack(en + "I", gh[:4])[0] == 0xA1B23C4D
        linktype = struct.unpack(en + "I", gh[20:24])[0]
        while True:
            rh = fh.read(16)
            if len(rh) < 16:
                break
            s, us, incl, _ = struct.unpack(en + "IIII", rh)
            yield s + us / (1e9 if nano else 1e6), fh.read(incl), linktype


# ---------------------------------------------------------------------------
# analysis / summary

class Tracker:
    """Learns which MAC addresses are DS consoles and keeps statistics."""

    def __init__(self):
        self.ds = set()
        self.events = []  # (ts, tsft, cls, src, seq, rate, length)
        self.beacon_info = defaultdict(set)

    def learn(self, f):
        if has_nintendo_ie(f):
            self.ds.add(bytes(f.a2))
            return True
        if f.ftype == 2 and (f.a1[:3] == MP_PREFIX or (f.a3 and f.a3[:3] == MP_PREFIX)) and f.a2:
            self.ds.add(bytes(f.a2))
            return True
        return False

    def relevant(self, f):
        if f.ftype == 2 and (f.a1[:3] == MP_PREFIX or (f.a3 and f.a3[:3] == MP_PREFIX)):
            return True
        for a in (f.a1, f.a2, f.a3):
            if a and (bytes(a) in self.ds or a[:3] == NINTENDO_OUI):
                return True
        return False

    def add(self, f):
        self.events.append((f.ts, f.tsft, f.cls, bytes(f.a2) if f.a2 else None, f.seq, f.rate, len(f.raw)))
        if f.cls == "beacon":
            self.beacon_info[bytes(f.a2)].add(f.body[8:12].hex() if len(f.body) >= 12 else "")

    def summary(self):
        out = ["", "=" * 70, "SUMMARY", "=" * 70]
        c = Counter(e[2] for e in self.events)
        out.append("frames by class: " + ", ".join("%s=%d" % kv for kv in sorted(c.items())))
        out.append("DS addresses seen: " + (", ".join(mac(m) for m in sorted(self.ds)) or "none"))
        rates = Counter(e[5] for e in self.events if e[5])
        if rates:
            out.append("rates (Mb/s): " + ", ".join("%g=%d" % kv for kv in sorted(rates.items())))
        # sequence-number deltas for consecutive MP-CMD frames from the same source
        last = {}
        deltas = Counter()
        for e in self.events:
            if e[2] == "MP-CMD" and e[3] is not None and e[4] is not None:
                if e[3] in last:
                    deltas[(e[4] - last[e[3]]) % 4096] += 1
                last[e[3]] = e[4]
        if deltas:
            out.append("MP-CMD seq deltas (notes say +2): " + ", ".join("+%d x%d" % kv for kv in deltas.most_common(6)))
        # beacon interval
        lastb = {}
        bint = []
        for e in self.events:
            if e[2] == "beacon" and e[3] is not None:
                if e[3] in lastb:
                    t0 = lastb[e[3]]
                    t1 = e[1] if e[1] is not None else None
                    bint.append((t1 - t0[1]) / 1000.0 if t1 is not None and t0[1] is not None else (e[0] - t0[0]) * 1000)
                lastb[e[3]] = (e[0], e[1])
        if bint:
            bint.sort()
            out.append("beacon spacing ms: min %.1f median %.1f max %.1f (n=%d)" % (
                bint[0], bint[len(bint) // 2], bint[-1], len(bint)))
        # inter-frame gaps between consecutive MP frames (TSFT, microseconds)
        gaps = defaultdict(list)
        prev = None
        for e in self.events:
            if e[2].startswith("MP-"):
                if prev and e[1] is not None and prev[1] is not None:
                    gaps[(prev[2], e[2])].append(e[1] - prev[1])
                prev = e
        for k, v in sorted(gaps.items()):
            v.sort()
            out.append("gap %s -> %s (us, start-to-start): min %d median %d max %d (n=%d)" % (
                k[0], k[1], v[0], v[len(v) // 2], v[-1], len(v)))
        sizes = defaultdict(Counter)
        for e in self.events:
            if e[2].startswith("MP-"):
                sizes[e[2]][e[6]] += 1
        for k, v in sorted(sizes.items()):
            out.append("%s frame sizes: %s" % (k, ", ".join("%dB x%d" % kv for kv in v.most_common(6))))
        return "\n".join(out)


def analyze_stream(frames, show_all=False, emit=print, tracker=None, t0=None):
    tr = tracker or Tracker()
    for f in frames:
        if f is None:
            continue
        if t0 is None:
            t0 = f.ts
        tr.learn(f)
        if show_all or tr.relevant(f):
            tr.add(f)
            emit(describe(f, t0))
    return tr


# ---------------------------------------------------------------------------
# live capture

def run(cmd, check=False):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError("%s failed: %s" % (" ".join(cmd), r.stderr.strip()))
    return r


def setup_monitor(iface, hwaddr=None):
    run(["nmcli", "dev", "set", iface, "managed", "no"])  # ignore failure
    run(["ip", "link", "set", iface, "down"], check=True)
    if hwaddr:
        # the hardware only ACKs frames sent to its own MAC, so take the host MAC (best effort)
        if run(["ip", "link", "set", "dev", iface, "address", hwaddr]).returncode != 0:
            print("# could not set MAC %s on %s; the DS will not get hardware ACKs" % (hwaddr, iface))
    run(["iw", "dev", iface, "set", "type", "monitor"], check=True)
    run(["ip", "link", "set", iface, "up"], check=True)


def set_channel(iface, ch):
    return run(["iw", "dev", iface, "set", "channel", str(ch)]).returncode == 0


def open_sock(iface):
    s = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.ntohs(3))
    s.bind((iface, 0))
    s.setblocking(False)
    return s


def recover(args, emit):
    """The USB adapter can drop off the bus (kernel: 'error -71'). Wait for it and set it up again."""
    for attempt in range(60):
        time.sleep(1)
        if not os.path.exists("/sys/class/net/%s" % args.iface):
            continue
        time.sleep(2)  # let the driver finish probing
        try:
            setup_monitor(args.iface)
            s = open_sock(args.iface)
            emit("# adapter is back (attempt %d), monitor mode restored" % (attempt + 1))
            return s
        except Exception as e:  # noqa: BLE001
            emit("# re-setup failed: %s" % e)
    return None


def capture(args):
    if os.geteuid() != 0:
        sys.exit("capture needs root: sudo python3 %s capture --iface %s ..." % (sys.argv[0], args.iface))
    if not args.no_setup:
        setup_monitor(args.iface)
    sock = open_sock(args.iface)

    pcap = open(args.out + ".pcap", "wb")
    pcap.write(pcap_header(127))
    logf = open(args.out + ".log", "w")

    def emit(s):
        print(s)
        logf.write(s + "\n")
        logf.flush()

    markers = []

    def stdin_thread():
        for line in sys.stdin:
            line = line.strip()
            if line:
                markers.append(line)

    threading.Thread(target=stdin_thread, daemon=True).start()

    if args.channel:
        channels = [args.channel]
    elif args.channels:
        channels = [int(x) for x in args.channels.split(",")]
    else:
        channels = list(range(1, 14))
    locked = bool(args.channel)
    cur_ch = channels[0]
    set_channel(args.iface, cur_ch)
    ci = 0
    next_hop = time.time() + args.dwell
    tr = Tracker()
    t0 = time.time()
    emit("# capturing on %s, %s. Type a label + Enter to add a marker. Ctrl-C to stop."
         % (args.iface, "channel %d" % args.channel if args.channel else "hopping channels " + ",".join(map(str, channels))))
    stats = Counter()
    ch_frames = Counter()   # every frame heard, per channel (adapter sanity check)
    other_ssids = set()     # SSIDs of ordinary access points heard
    try:
        while True:
            now = time.time()
            if not locked and now >= next_hop and len(channels) > 1:
                ci = (ci + 1) % len(channels)
                cur_ch = channels[ci]
                set_channel(args.iface, cur_ch)
                next_hop = now + args.dwell
            while markers:
                emit("%9.3f ---- MARKER: %s" % (time.time() - t0, markers.pop(0)))
            r, _, _ = select.select([sock], [], [], 0.05)
            if not r:
                continue
            try:
                pkt, _ = sock.recvfrom(4096)
            except BlockingIOError:
                continue
            except OSError as e:
                emit("%9.3f # adapter error: %s - waiting for it to come back" % (time.time() - t0, e))
                try:
                    sock.close()
                except OSError:
                    pass
                sock = recover(args, emit)
                if sock is None:
                    emit("# giving up: adapter did not come back within 60 s")
                    break
                set_channel(args.iface, cur_ch)
                continue
            ts = time.time()
            f = parse_frame(ts, pkt, 127)
            if f is None:
                continue
            stats["seen"] += 1
            ch_frames[freq_to_ch(f.freq) or cur_ch] += 1
            if f.cls == "beacon" and not has_nintendo_ie(f) and len(f.body) >= 14:
                for tag, d, _t in parse_ies(f.body[12:]):
                    if tag == 0:
                        other_ssids.add(d.decode("latin1") or "<hidden>")
                        break
            is_new = tr.learn(f)
            if not locked and (is_new or (f.ftype == 2 and f.a1[:3] == MP_PREFIX)):
                ch = freq_to_ch(f.freq)
                if ch and not args.no_lock:
                    locked = True
                    emit("# DS traffic found on channel %d - staying here" % ch)
            if args.all or tr.relevant(f):
                tr.add(f)
                pcap.write(pcap_record(ts, pkt))
                pcap.flush()
                emit(describe(f, t0))
    except KeyboardInterrupt:
        pass
    finally:
        emit("# total frames seen on the air: %d" % stats["seen"])
        emit("# frames heard per channel: " + (", ".join("ch%s=%d" % kv for kv in sorted(ch_frames.items(), key=lambda kv: str(kv[0]))) or "none"))
        emit("# ordinary access points heard (%d): %s" % (len(other_ssids), ", ".join(sorted(other_ssids)[:12]) or "none"))
        emit(tr.summary())
        logf.close()
        pcap.close()
        print("\nwrote %s.pcap and %s.log" % (args.out, args.out))


def analyze(args):
    frames = (parse_frame(ts, pkt, lt) for ts, pkt, lt in read_pcap(args.pcap))
    tr = analyze_stream(frames, show_all=args.all)
    print(tr.summary())


# ---------------------------------------------------------------------------
# host beacon (fake PictoChat room)
#
# Layout copied from a real DS Lite beacon (capture of a real room) and the firmware (see PROTOCOL.md, section 3).

LCG_A, LCG_C = 0x5EEDF715, 0x1B0CB173   # arm9 fn 0x0236B24C / 0x0236B1AC
BEACON_UPDATE_EVERY = 25                # beacons per counter step (about 2.5 s on a real DS)


class HostState:
    """Evolving beacon content: counter, LCG nonce, interval jitter, DTIM toggle, 802.11 sequence."""

    def __init__(self, seed=None, room=0, counter=1):
        self.lcg = (seed if seed is not None else int.from_bytes(os.urandom(4), "little")) & 0xFFFFFFFF
        self.room = room              # 0-3 = room A-D (game-info byte 20)
        self.members = 1              # host + joined clients (game-info byte 21)
        self.status = 0x2348          # game-info bytes 16-17: 0x2348 = room just opened, 0xBD8A once a client connected
        self.counter = counter
        self.seq = 0
        self.nbeacons = 0
        self.interval = 100
        self.frozen = False           # a client has connected: keep interval and counter fixed, as a real host does
        self.tsf = 0                  # beacon timestamp, advanced by exactly interval * 1024 us per beacon
        self._update(first=True)

    def _step(self):
        self.lcg = (LCG_A * self.lcg + LCG_C) & 0xFFFFFFFF
        return self.lcg

    def _update(self, first=False):
        if not first:
            self.counter = (self.counter + 1) & 0xFFFF
        # a real DS advances the LCG 4 times per update (verified on the capture); the first step is the nonce
        self.nonce = self._step() & 0xFFFF
        for _ in range(3):
            r = self._step()
        # real intervals were 100,102,...,108; how the DS derives them from the LCG is not verified
        self.interval = 100 + 2 * ((r >> 16) % 5)

    def next_mgmt(self):
        seq = self.seq
        self.seq = (self.seq + 1) & 0xFFF
        return 0, seq

    def on_members_changed(self):
        """A client connected or left: freeze the beacon timing and refresh only the nonce (arm9 FUN_0236AA6C)."""
        self.frozen = True
        self.nonce = self._step() & 0xFFFF

    def next(self):
        """Return (dtim_count, seq) for the next beacon and advance the state."""
        if self.nbeacons and self.nbeacons % BEACON_UPDATE_EVERY == 0 and not self.frozen:
            self._update()
        self.tsf += self.interval * 1024
        dtim = self.nbeacons & 1
        seq = self.seq
        self.nbeacons += 1
        self.seq = (self.seq + 1) & 0xFFF
        return dtim, seq


def gameinfo(counter, nonce, room=0, members=1, status=0x2348):
    """The 24 game-info bytes of a PictoChat beacon (arm9 fn 0x0236B1AC builds bytes 16-23):
    01 00 00 00 00 00 00 00 | counter | 08 01 c0 00 c0 00 | status | nonce | room | members | 04 00.
    status is 0x2348 when the room is opened and 0xBD8A after a client has connected; members counts the host."""
    return (bytes([1, 0, 0, 0, 0, 0, 0, 0]) + struct.pack("<H", counter) + bytes.fromhex("0801c000c000") +
            struct.pack("<HH", status, nonce) + bytes([room & 0xFF, members & 0xFF]) + struct.pack("<H", 4))


def build_beacon(src, channel, st, tsf=0):
    """802.11 beacon (no FCS) shaped like the DS Lite's. Advances st."""
    dtim, seq = st.next()
    vendor = NINTENDO_OUI + b"\x00" + struct.pack("<HH", 0x000A, 0) + gameinfo(st.counter, st.nonce, st.room, st.members, st.status)
    body = (struct.pack("<QHH", tsf, st.interval, 0x0021) +
            b"\x01\x02\x82\x84" + b"\x03\x01" + bytes([channel]) +
            b"\x05\x05" + bytes([dtim, 2, 0, 0, 0]) +
            b"\xdd" + bytes([len(vendor)]) + vendor)
    return _dot11(0x80, 0x00, BROADCAST, src, src, seq, body)


def tx_radiotap(rate_mbps=2):
    """Minimal radiotap header for injection: just the rate."""
    return struct.pack("<BBHIB", 0, 0, 9, 1 << 2, int(rate_mbps * 2))


def _lcg4(x):
    for _ in range(4):
        x = (LCG_A * x + LCG_C) & 0xFFFF
    return x


def build_cmd(src, seq, clienttime, mask, word=0, data=b""):
    """Host multiplay poll: data+CF-Poll, FromDS, to 03:09:BF:00:00:00 (layout from melonDS + firmware fn 0x023091DC)."""
    return _dot11(0x28, 0x02, MP_CMD, src, src, seq, struct.pack("<HHH", clienttime, mask, word) + data)


def build_hostpkt(ptype, value, key, frameno, body_len=108):
    """Port 0xC handshake packet, shaped like the DS's own type-6 hello (see PROTOCOL.md, sections 4 and 6).

    Returns (word, data): `word` is the third CMD body word (the DS reply starts with 0x8c34 in the same place),
    `data` is the rest. Whole body: 0-1 word 0x8c34, 2-3 type, 4-5 length (0x68), 6-9 LCG random, 10-105 the 0x60
    bytes of user info (all zero unless `key` is given, which is then written at 16-19), 106-107 = `frameno`.
    From the ARM9 firmware: a client sends type 6; the host answers type 4; a client in state 8/9 that gets a
    type 4 goes to state 6 (joined). Types 5 is ignored by clients. The 0x60-byte contents are not known.
    """
    b = bytearray(body_len)
    b[0], b[1] = 0x34, 0x8C
    struct.pack_into("<HH", b, 2, ptype, body_len - 4)
    struct.pack_into("<I", b, 6, value & 0xFFFFFFFF)
    if key:
        struct.pack_into("<I", b, 16, key & 0xFFFFFFFF)
    struct.pack_into("<H", b, body_len - 2, frameno & 0xFFFF)
    return struct.unpack_from("<H", b, 0)[0], bytes(b[2:])


def wm_entry(body):
    """Split a multiplay body into (port, payload) using the WM word: flags | port << 8 | payload_len / 2.
    Bits 8-11 = port (0xC-0xE), bits 0-7 = payload length in halfwords; bit 12 is set in host words and bit 15 is
    set on some frames of both sides (meaning unknown, it must NOT be required: the client's first hello comes with
    bit 15 clear). Length 0 is an idle entry. Returns None for bodies too short to hold a word."""
    if len(body) < 2:
        return None
    w = struct.unpack_from("<H", body, 0)[0]
    return (w >> 8) & 0xF, bytes(body[2:2 + (w & 0xFF) * 2])


def port_packet(port, payload):
    """(word, data) for the CMD body: WM word, payload, then the u16 tail 0x0001 the DS also appends."""
    return 0x8000 | (port << 8) | (len(payload) // 2), payload + b"\x01\x00"


def host_word_packet(port, payload):
    """(word, data) for a host poll as the real host sends it: word 0x1000 | port << 8 | len/2, payload, no tail."""
    return 0x1000 | (port << 8) | (len(payload) // 2), payload


def type4_payload(value, info=bytes(0x60)):
    """Port 0xC host-info packet (answer to the client's type 6)."""
    return struct.pack("<HHI", 4, 0x68, value & 0xFFFFFFFF) + info


def type1_payload(cid, length, rand):
    """Port 0xD token grant: type 1, 0x14 bytes, sender id at +4, LENGTH OF THE MESSAGE that sender will send at +8
    (84 for member messages, 10276 for a drawing; the receiver sizes its copy from it, a wrong value crashes the DS),
    random at +0x10 (the requester's random echoed, or the host's own for its own messages)."""
    return struct.pack("<HHBBHIII", 1, 0x14, cid, 0, 0xFFFF, length & 0xFFFFFFFF, 0, rand & 0xFFFFFFFF)


def host_join_message(src, name, msg_id=0):
    """Port 0xE fragment modelled on the DS's own first message (PROTOCOL.md section 7): data = 03 00, MAC with each
    16-bit pair byte-swapped, UTF-16LE name, zeros, 04 00 05 01 at the end. The meaning of the fields is a guess."""
    sw = bytes(src[i ^ 1] for i in range(6))
    data = bytearray(84)
    data[0:2] = b"\x03\x00"
    data[2:8] = sw
    nm = name.encode("utf-16-le")[:20]
    data[8:8 + len(nm)] = nm
    data[80:84] = bytes([4, 0, 5, 1])
    return struct.pack("<HHBBBBI", 2, 12 + len(data), msg_id, 0, len(data), 1, 0) + bytes(data)


def build_ack(src, seq, cmdcount, fail):
    """Host ACK after the reply window, to 03:09:BF:00:00:03 (cmdcount at body 0, client-fail mask at body 2)."""
    return _dot11(0x18, 0x02, MP_ACK, src, src, seq, struct.pack("<HH", cmdcount, fail))   # 4-byte body, like the real host



def swap16(b):
    """Swap the two bytes of every 16-bit pair (how MACs appear in the multiplay roster and messages)."""
    return bytes(b[i ^ 1] for i in range(len(b)))


def member_info_message(kind, mac_, name, color=2):
    """84-byte `03 xx` member message (kind 0 = joining, 1 = ready): 03 kind | MAC swapped | UTF-16 name (20 bytes) |
    zeros | colour u16 | 01 01. Layout copied from the real host/client traces (PROTOCOL.md section 7)."""
    d = bytearray(84)
    d[0], d[1] = 0x03, kind
    d[2:8] = swap16(mac_)
    nm = name.encode("utf-16-le")[:20]
    d[8:8 + len(nm)] = nm
    d[80:84] = struct.pack("<HBB", color, 1, 1)
    return bytes(d)


def fragments(msg_id, data, junk=0x69):
    """Split a message into port-0xE type-2 payloads (160 data bytes per fragment)."""
    out = []
    for off in range(0, max(len(data), 1), 160):
        chunk = data[off:off + 160]
        last = 1 if off + 160 >= len(data) else 0
        out.append(struct.pack("<HHBBBBI", 2, 12 + len(chunk), msg_id, junk, len(chunk), last, off) + chunk)
    return out


def chat_message(mac_, image, header_tail=bytes.fromhex("0104090b101b")):
    """A drawn chat message: 03 02 | MAC swapped | 00 04 00 00 00 00 | 6 bytes (unknown, copied from real messages) |
    16 zero bytes | picture: 256 px wide, 16-px lines (1-5 lines, 2048 bytes each), 8x8 tiles of 4bpp, 32 tiles per
    row. Verified on an emulated DS (PROTOCOL.md section 7). Real DSes send only the lines that contain ink."""
    assert len(image) % 2048 == 0 and 0 < len(image) <= 10240
    return b"\x03\x02" + swap16(mac_) + bytes.fromhex("000400000000") + header_tail + bytes(16) + image


def bitmap_to_image(pixels):
    """pixels: 80 rows of 256 palette indices (0-15) -> 10240 bytes in the 8x8-tile 4bpp layout used by PictoChat."""
    out = bytearray()
    for ty in range(10):
        for tx in range(32):
            for y in range(8):
                row = pixels[ty * 8 + y]
                for x in range(0, 8, 2):
                    out.append((row[tx * 8 + x] & 15) | ((row[tx * 8 + x + 1] & 15) << 4))
    return bytes(out)


class MultiplayHost:
    """The multiplay (WM port 0xC/0xD/0xE) side of a PictoChat host, following the traces of a real host (item 19).

    I/O-free: feed it what the clients send (`on_reply`) and ask it what to put in the next poll (`next_entry`).
    Per client the flow is: assoc -> (idle roster packets) -> client hello (0xC type 6) -> host info (0xC type 4)
    -> host token grant (0xD type 1, id 0) -> host's own member message (`03 00`) -> client token request (0xD type 0) ->
    grant (0xD type 1, echo) -> client message -> verbatim echo -> after the client's `03 00` the host sends `03 01`."""

    def __init__(self, host_mac, name="py", color=2, rand=None, repeats=5, log=None):
        self.mac, self.name, self.color = bytes(host_mac), name, color
        self.rand = rand or (lambda: int.from_bytes(os.urandom(4), "little"))
        self.repeats = repeats
        self.log = log or (lambda m: None)
        self.clients = {}          # aid -> {"mac": bytes, "stage": str}
        self.queue = []            # [port, payload, repeats_left, announced]
        self.held = []             # GUI packets waiting for a joined client
        self.idle_n = 0
        self.idle_val = self.rand()
        self.ctr = {}              # port -> poll counter (the first u16 of the 4-byte tail of every poll)
        self.seen = {}             # (port, payload) -> last time handled
        self.asm = {}              # sender id -> bytearray
        self.messages = []         # complete messages received: (sender id, bytes)
        self.sent_ready = set()

    # ---- builders ----
    def roster(self):
        info = bytearray(0x60)
        macs = [self.mac] + [c["mac"] for _a, c in sorted(self.clients.items())]
        for i, m in enumerate(macs[:15]):
            info[i * 6:i * 6 + 6] = swap16(m)
        return bytes(info)

    def _type(self, t, val):
        return struct.pack("<HHI", t, 0x68, val & 0xFFFFFFFF) + self.roster()

    def put(self, port, payload, repeats=None, front=False, min_sent=5):
        """Queue a packet. It goes out on up to `repeats` polls; once the clients have answered a poll carrying it and
        it has been sent at least `min_sent` times, the rest is skipped (see confirm)."""
        item = [port, payload, repeats or self.repeats, False, min_sent, 0]
        if front:
            self.queue.insert(0, item)
        else:
            self.queue.append(item)

    def put_message(self, data, msg_id=0):
        """Queue a whole message from the host: token grant to itself, then each fragment."""
        self.put(0xD, type1_payload(msg_id, len(data), self.rand()))
        frs = fragments(msg_id, data, junk=self.rand() & 0xFF)
        for i, fr in enumerate(frs):
            # middle fragments may move on as soon as a client confirmed them; the first and the last keep the full
            # count, because PictoChat can drop a packet it received while it was still busy with its previous step
            self.put(0xE, fr, min_sent=5 if i in (0, len(frs) - 1) else 1)

    def send_chat(self, image):
        # like a real DS: send only up to the last 16-px line that contains ink (2048 bytes per line)
        lines = max([i + 1 for i in range(len(image) // 2048) if any(image[i * 2048:(i + 1) * 2048])] or [1])
        image = image[:lines * 2048]
        msg = chat_message(self.mac, image)
        if any(c["stage"] == "joined" for c in self.clients.values()):
            self.put_message(msg)
            self.log("queued chat message (%d bytes, %d fragments)" % (len(msg), (len(msg) + 159) // 160))
        else:
            self.held.append(("msg", msg))
            self.log("no client has joined yet; holding the chat message until one has")
        return msg

    # ---- events ----
    def on_assoc(self, aid, mac_):
        self.clients[aid] = {"mac": bytes(mac_), "stage": "assoc"}
        self.sent_ready.discard(aid)
        self.asm.clear()
        self.queue.clear()
        self.log("client %s assoc, AID %d" % (":".join("%02x" % x for x in mac_), aid))

    def on_deauth(self, aid):
        self.clients.pop(aid, None)
        if not self.clients:
            self.queue.clear()

    def on_reply(self, aid, port, pl, now=None):
        now = time.time() if now is None else now
        c = self.clients.get(aid)
        if c is None or len(pl) < 2:
            return
        ptype = struct.unpack_from("<H", pl, 0)[0]
        if now - self.seen.get((port, pl), -9) < 0.0:
            return
        if port == 0xC and ptype == 6 and c["stage"] == "assoc":
            c["stage"] = "hello"
            self.put(0xC, self._type(4, self.rand()), 9)
            self.put_message(member_info_message(0, self.mac, self.name, self.color))
            self.log("client %d hello -> host info + host member message" % aid)
        elif port == 0xD and ptype == 0 and len(pl) >= 20:
            cid, want, echo = pl[4], struct.unpack_from("<I", pl, 8)[0], struct.unpack_from("<I", pl, 16)[0]
            if now - self.seen.get(("D0", echo), -9) > 0.5:
                self.seen[("D0", echo)] = now
                self.put(0xD, type1_payload(cid, want, echo))      # after what is already queued, as the real host does
                self.log("token request from id %d for a %d-byte message (random %08x) -> grant" % (cid, want, echo))
        elif port == 0xD and ptype == 3:
            self.log("ack from client %d" % aid)
        elif port == 0xE and ptype == 2 and len(pl) >= 12:
            _t, _l, mid, _x, flen, last, off = struct.unpack_from("<HHBBBBI", pl, 0)
            if now - self.seen.get(("E", pl), -9) > 0.5:
                self.seen[("E", pl)] = now
                self.put(0xE, pl)                                   # rebroadcast verbatim
                buf = self.asm.setdefault(mid, bytearray())
                if len(buf) < off + flen:
                    buf.extend(bytes(off + flen - len(buf)))
                buf[off:off + flen] = pl[12:12 + flen]
                if last:
                    msg = bytes(self.asm.pop(mid))
                    self.messages.append((mid, msg))
                    self.log("message from id %d complete: %d bytes, type %02x %02x" % (mid, len(msg), msg[0], msg[1]))
                    if msg[:2] == b"\x03\x00" and aid not in self.sent_ready:
                        self.sent_ready.add(aid)
                        self.put_message(member_info_message(1, self.mac, self.name, self.color))
                    elif msg[:2] == b"\x03\x01":
                        # the real host answers a newcomer's 03 01 with its own 03 01 once more
                        self.put_message(member_info_message(1, self.mac, self.name, self.color))
                        c["stage"] = "joined"
                        for kind, data in self.held:
                            if kind == "msg":
                                self.put_message(data)
                        self.held.clear()
                        self.log("client %d joined" % aid)

    def confirm(self):
        """All addressed clients answered the poll that carried the current packet: it has been delivered, so move on
        to the next packet instead of sending the remaining repeats."""
        it = getattr(self, "last_item", None)
        if it is not None and self.queue and self.queue[0] is it and it[5] >= it[4]:
            self.queue.pop(0)
        self.last_item = None

    def undo_entry(self, port):
        """Take back the counter bump of the entry just handed out (used when the poll is sent empty instead)."""
        self.ctr[port] = max(0, self.ctr.get(port, 1) - 1)

    def next_entry(self):
        """(port, payload) for the next poll: the head of the queue (each packet is repeated), else an idle roster."""
        self.last_item = None
        if self.queue:
            item = self.queue[0]
            item[2] -= 1
            item[5] += 1
            first = not item[3]
            item[3] = True
            if item[2] <= 0:
                self.queue.pop(0)
            port, payload = item[0], item[1]
            self.last_item = item
        else:
            first = False
            self.idle_n += 1
            if self.idle_n % 5 == 1:
                self.idle_val = self.rand()
            port, payload = 0xC, self._type(5, self.idle_val)
        self.ctr[port] = (self.ctr.get(port, 0) % 0xFFFF) + 1
        return port, payload, first, self.ctr[port]


class BridgeSock:
    """Socket-like link to the melonDS test harness (`harness ... N PORT`), standing in for the radio.

    host() sends `tx_radiotap + 802.11 frame` and receives `radiotap + 802.11 frame` as with a monitor-mode socket;
    here the frames travel over TCP: [u32 n][u8 kind][12-byte TX header + frame + 4 FCS bytes] to the emulator
    (kind 0 normal, 1 CMD, 3 ACK) and [u32 n][u8 kind][u16 aid][u64 ts][same] back (kind 0 normal, 2 reply)."""
    RT = bytes([0, 0, 8, 0, 0, 0, 0, 0])

    def __init__(self, port, channel, host_mac=bytes(6)):
        try:
            self.s = socket.create_connection(("127.0.0.1", port), timeout=5)
        except OSError as e:
            sys.exit("cannot reach the emulator bridge on 127.0.0.1:%d: %s\nStart the harness first: "
                     "emu/start.sh OUT.pcap 1 %d (or use emu/run_bridge.sh)." % (port, e, port))
        self.s.settimeout(None)
        self.channel = channel
        self.host_mac = bytes(host_mac)
        self.client_mac = bytes(6)       # last station MAC seen, used for blank replies
        self.buf = b""
        self.pending = []
        self.want_poll = False          # lockstep: the emulated client is waiting for a poll right now

    def fileno(self):
        return self.s.fileno()

    def close(self):
        self.s.close()

    def send(self, data):
        frame = bytes(data[9:])                       # strip tx_radiotap()
        kind = 1 if frame[4:10] == MP_CMD else 3 if frame[4:10] == MP_ACK else 0
        hdr = bytes(8) + bytes([0x14, self.channel]) + struct.pack("<H", len(frame) + 4)
        payload = bytes([kind]) + hdr + frame + bytes(4)
        self.s.sendall(struct.pack("<I", len(payload)) + payload)
        return len(data)

    def _fill(self):
        try:
            d = self.s.recv(65536, socket.MSG_DONTWAIT)
        except BlockingIOError:
            d = None
        if d == b"":
            raise OSError("emulator closed the bridge")
        if d:
            self.buf += d
        while len(self.buf) >= 4:
            n = struct.unpack_from("<I", self.buf, 0)[0]
            if len(self.buf) < 4 + n:
                break
            msg, self.buf = self.buf[4:4 + n], self.buf[4 + n:]
            kind, aid, ts = struct.unpack_from("<BHQ", msg, 0)
            body = msg[11:]
            if kind == 9:
                self.want_poll = True
                continue
            if len(body) >= 12 + 24 + 4:
                frame = body[12:-4]
                if kind == 0 and len(frame) >= 16:
                    self.client_mac = bytes(frame[10:16])
                self.pending.append(self.RT + frame)
            elif kind == 2 and not body:
                # blank reply (the client had nothing to send): still an answer, as the DS hardware counts it
                blank = bytes([0x18, 0x01, 0, 0]) + self.host_mac + self.client_mac + MP_REPLY + bytes(2)
                self.pending.append(self.RT + blank)

    def recvfrom(self, n):
        self._fill()
        if not self.pending:
            raise BlockingIOError()
        return self.pending.pop(0), None


class SerialSock:
    """Socket-like link to the ESP32-C3 radio modem (esp32c3_modem/, USB-C = /dev/ttyACM0), standing in for a
    monitor-mode adapter. No root needed.

    USB framing both ways: AA 55 | type | u16 len LE | payload | xor8 of type, len and payload (see modem.c).
    To the modem: 'C' channel, mac[6], rate, all; 'T' 802.11 frame without FCS; 'P' ping.
    From the modem: 'R' rssi i8, rate u8 (wifi_phy_rate_t), u32 timestamp, frame; 'S' status text; 'E' TX error.
    send() takes `tx_radiotap + frame` like a monitor socket; recvfrom() returns `radiotap + frame`. Own frames are
    looped back (as a monitor socket does) so they still land in the pcap.

    Multiplay offload (firmware that reports `mp=1`): the modem runs the poll / reply / ACK cycle itself. The host
    pushes packets with mp_push(), keeps the modem's client list current with mp_clients() and gets 'M' reports
    (queue length, poll and reply counters) in self.mpstats."""
    RATE_500K = {0: 2, 1: 4, 2: 11, 3: 22, 5: 4, 6: 11, 7: 22, 8: 96, 9: 48, 10: 24, 11: 12, 12: 108, 13: 72,
                 14: 36, 15: 18}
    PHY_2M_LONG = 1

    def __init__(self, port, channel, host_mac, forward_all=False, emit=print):
        try:
            import serial  # pyserial
        except ImportError:
            sys.exit("--serial needs pyserial: .venv/bin/pip install -r requirements.txt, then run with .venv/bin/python")
        try:
            self.ser = serial.Serial(port, 115200, timeout=0, write_timeout=1, exclusive=True)
        except serial.SerialException as e:
            sys.exit("cannot open %s: %s\nIs the ESP32-C3 plugged in (ls /dev/ttyACM*)? Is another host already using "
                     "the port? Without root your user must be in the dialout group (sudo usermod -aG dialout $USER, "
                     "then log in again)." % (port, e))
        self.channel = channel
        self.freq = 2484 if channel == 14 else 2407 + 5 * channel
        self.emit = emit
        self.buf = bytearray()
        self.pending = []
        self.tx_errors = 0
        self.want_poll = False
        self.mp = False               # firmware supports the multiplay offload
        self.mpstats = None
        self.epoch = 0
        self.pushed = 0               # packets pushed since the last mp_clear (mod 65536)
        self.ser.reset_input_buffer()
        self._write(b"C", bytes([channel]) + bytes(host_mac) + bytes([self.PHY_2M_LONG, 1 if forward_all else 0]))
        deadline = time.time() + 5
        self.ready = False
        while time.time() < deadline and not self.ready:
            select.select([self.ser], [], [], 0.1)
            self._fill()
        if not self.ready:
            raise OSError("no answer from the ESP32 modem on %s (flashed? see esp32c3_modem/)" % port)

    def fileno(self):
        return self.ser.fileno()

    def close(self):
        self.ser.close()

    def _write(self, kind, payload):
        n = len(payload)
        x = kind[0] ^ (n & 0xFF) ^ (n >> 8)
        for b in payload:
            x ^= b
        self.ser.write(b"\xaa\x55" + kind + struct.pack("<H", n) + payload + bytes([x]))

    def send(self, data):
        frame = bytes(data[9:])                       # strip tx_radiotap()
        self._write(b"T", frame)
        self.pending.append(struct.pack("<BBHIB", 0, 0, 9, 1 << 2, 4) + frame)   # loopback copy, like a monitor socket
        return len(data)

    def _fill(self):
        d = self.ser.read(65536)
        if d:
            self.buf += d
        b = self.buf
        while True:
            i = b.find(b"\xaa\x55")
            if i < 0:
                del b[:max(0, len(b) - 1)]
                return
            if i:
                del b[:i]
            if len(b) < 5:
                return
            kind, n = b[2], struct.unpack_from("<H", b, 3)[0]
            if n > 4096:
                del b[:2]
                continue
            if len(b) < 6 + n:
                return
            payload = bytes(b[5:5 + n])
            x = kind ^ (n & 0xFF) ^ (n >> 8)
            for c in payload:
                x ^= c
            if x != b[5 + n]:
                del b[:2]                             # not a real frame start: resync
                continue
            del b[:6 + n]
            self._handle(kind, payload)

    def _handle(self, kind, p):
        if kind == ord("R") and len(p) > 6:
            rssi, rate = struct.unpack_from("<bB", p, 0)
            rt = struct.pack("<BBHIBBHHb", 0, 0, 15, (1 << 2) | (1 << 3) | (1 << 5),
                             self.RATE_500K.get(rate, 0), 0, self.freq, 0x00A0, rssi)
            self.pending.append(rt + p[6:])
        elif kind == ord("S"):
            text = p.decode("ascii", "replace")
            if text.startswith("ready"):
                self.ready = True
                self.mp = " mp=1" in text
            self.emit("# esp32: " + text)
        elif kind == ord("M") and len(p) >= 25:
            epoch, qlen, queued, polls, replies, acks, done, txerr = struct.unpack_from("<BHHIIIII", p, 0)
            self.mpstats = dict(epoch=epoch, qlen=qlen, queued=queued, polls=polls, replies=replies, acks=acks,
                                done=done, txerr=txerr)
        elif kind == ord("E") and len(p) >= 6:
            self.tx_errors += 1
            if self.tx_errors <= 5 or self.tx_errors % 1000 == 0:
                err, fc = struct.unpack_from("<iH", p, 0)
                self.emit("# esp32: TX error %d (0x%x) for frame control %04x, %d errors so far"
                          % (err, err, fc, self.tx_errors))

    # ---- multiplay offload ----
    def mp_config(self, on, clienttime, cmd_word, cmdcount, period_us, reply_wait_us, cycle):
        self._write(b"m", struct.pack("<BHHHIIB", 1 if on else 0, clienttime, cmd_word, cmdcount,
                                      int(period_us), int(reply_wait_us), cycle))

    def mp_clients(self, aids, reopen=False):
        """aids: {mac bytes: aid}. reopen: start again with the opening empty poll (re-association)."""
        items = sorted(aids.items(), key=lambda kv: kv[1])
        self._write(b"A", bytes([len(items)]) + b"".join(bytes([a]) + bytes(m) for m, a in items) +
                    bytes([1 if reopen else 0]))

    def mp_push(self, port, payload, min_sent, max_sent):
        self._write(b"Q", bytes([port, max(0, min(255, min_sent)), max(1, min(255, max_sent))]) + bytes(payload))
        self.pushed = (self.pushed + 1) & 0xFFFF

    def mp_clear(self):
        self.epoch = (self.epoch + 1) & 0xFF
        self.pushed = 0
        self._write(b"X", bytes([self.epoch]))

    def mp_outstanding(self):
        """Packets pushed that the modem has not finished yet (queued there or still on the way over USB)."""
        m = self.mpstats
        if not m or m["epoch"] != self.epoch:
            return self.pushed
        return m["qlen"] + ((self.pushed - m["queued"]) & 0xFFFF)

    def recvfrom(self, n):
        if not self.pending:
            self._fill()
        if not self.pending:
            raise BlockingIOError()
        return self.pending.pop(0), None


class GuiBridge:
    """JSON-lines link between `host` and its clients: pictochat_gui.py, bots, the Home Assistant integration.

    host -> client: {"kind": "log"|"rx"|"tx"|"message"|"state"|"ready", ...}; client -> host: {"cmd": ...} (see
    handle_gui_cmd). Listens on 127.0.0.1 unless `bind` says otherwise. With a `token`, a client's first line must be
    {"cmd":"auth","token":"..."}; until then it receives nothing and its commands are ignored."""

    def __init__(self, port, cmdq, bind="127.0.0.1", token="", clock=time.time):
        self.cmdq = cmdq
        self.token = token
        self.clock = clock
        self.clients = []
        self.history = []          # replayed to a client that connects late
        self.lock = threading.Lock()
        srv = socket.socket()
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((bind, port))
        srv.listen(8)
        threading.Thread(target=self._accept, args=(srv,), daemon=True).start()

    def _accept(self, srv):
        while True:
            c, _ = srv.accept()
            threading.Thread(target=self._serve, args=(c,), daemon=True).start()

    def _serve(self, c):
        buf = b""
        if self.token:
            buf = self._auth(c)
            if buf is None:
                try:
                    c.sendall(b'{"kind":"error","text":"authentication failed"}\n')
                except OSError:
                    pass
                c.close()
                return
        wl = threading.Lock()
        wl.acquire()                                 # held by the replay until the backlog is out
        with self.lock:
            # "ready" ends the replay: everything a client reads before it is history, everything after it is live
            ready = {"kind": "ready", "t": self.clock(), "version": __version__}
            backlog = b"".join((json.dumps(o) + "\n").encode() for o in self.history + [ready])
            self.clients.append((c, wl))
        # replay outside the lock, so a client that does not read can never stall the host
        threading.Thread(target=self._replay, args=(c, wl, backlog), daemon=True).start()
        self._read(c, buf)

    def _auth(self, c):
        """Reads the first line and checks the token. Returns the bytes that followed that line, or None."""
        buf = b""
        c.settimeout(10)
        try:
            while b"\n" not in buf:
                d = c.recv(4096)
                if not d or len(buf) > 4096:
                    return None
                buf += d
            line, rest = buf.split(b"\n", 1)
            o = json.loads(line)
            given = str(o.get("token", "")) if isinstance(o, dict) and o.get("cmd") == "auth" else ""
        except (OSError, ValueError):
            return None
        return rest if hmac.compare_digest(given.encode(), self.token.encode()) else None

    def _replay(self, c, wl, backlog):
        try:
            c.sendall(backlog)                           # may block this helper thread only
        except OSError:
            pass
        wl.release()

    def _read(self, c, buf=b""):
        c.settimeout(None)
        try:
            while True:
                if b"\n" not in buf:
                    d = c.recv(4096)
                    if not d:
                        break
                    buf += d
                    if len(buf) > 1 << 20:      # no command is this long: a client that floods is dropped
                        break
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    try:
                        o = json.loads(line)
                    except ValueError:
                        continue
                    if not (isinstance(o, dict) and o.get("cmd") == "auth"):    # auth is handled in _serve
                        self.cmdq.put(o)
        except OSError:
            pass
        with self.lock:
            self.clients[:] = [e for e in self.clients if e[0] is not c]

    def send(self, obj):
        data = (json.dumps(obj) + "\n").encode()
        with self.lock:
            if obj.get("kind") in ("rx", "tx", "message", "log"):
                self.history.append(obj)
                del self.history[:-600]
            for e in list(self.clients):
                c, wl = e
                if not wl.acquire(blocking=False):
                    continue                     # backlog still being replayed: skip this line for that client
                try:
                    n = c.send(data, socket.MSG_DONTWAIT)   # never block the host loop
                    if n < len(data):
                        self.clients.remove(e)   # partial line: the client is not reading, drop it
                except BlockingIOError:
                    pass
                except OSError:
                    self.clients.remove(e)
                finally:
                    wl.release()


def host_reply(src, f, st, aid=1):
    """Answer a DS auth / assoc request (open system, accept). Returns a frame or None."""
    ta = bytes(f.a2)
    if f.cls == "auth" and len(f.body) >= 6:
        alg, trans, _status = struct.unpack_from("<HHH", f.body, 0)
        if trans == 1:
            _d, seq = st.next_mgmt()
            return _dot11(0xB0, 0x00, ta, src, src, seq, struct.pack("<HHH", alg, 2, 0))
    if f.cls == "assoc-req":
        _d, seq = st.next_mgmt()
        aid = 0xC000 | aid
        return _dot11(0x10, 0x00, ta, src, src, seq, struct.pack("<HHH", 0x0021, 0, aid) + b"\x01\x02\x82\x84")
    return None


def handle_gui_cmd(c, args, ml, src, emit, gui):
    """Commands from pictochat_gui.py."""
    cmd = c.get("cmd") if isinstance(c, dict) else None
    try:
        if cmd == "raw":        # {"cmd":"raw","port":14,"hex":"...","repeats":6}: payload on a port
            port, pl = int(c["port"]), bytes.fromhex(c["hex"])
            ml.put(port, pl, int(c.get("repeats", args.hello_repeats)))
            emit("# gui: queued raw port 0x%x payload %s" % (port, pl.hex()))
            gui("message", id=0, hex=pl.hex(), dir="tx")
        elif cmd == "announce":
            pl = member_info_message(0, src, c.get("name") or args.name, args.color)
            ml.put_message(pl)
            emit("# gui: queued host member message")
            gui("message", id=0, hex=pl.hex(), dir="tx")
        elif cmd == "set_name":  # {"cmd":"set_name","name":"..."}: nickname for the joins that follow (max 10)
            name = str(c["name"])[:10]
            ml.name = args.name = name
            emit("# gui: host nickname is now %r (shown to DSes that join from now on)" % name)
        elif cmd == "chat":     # {"cmd":"chat","pixels":[[...]*256]*80} palette indices
            px = c["pixels"]
            if not (isinstance(px, list) and len(px) == 80
                    and all(isinstance(r, list) and len(r) == 256 for r in px)):
                raise ValueError("pixels must be 80 rows of 256 palette indices")
            msg = ml.send_chat(bitmap_to_image(px))
            gui("message", id=0, hex=msg.hex(), dir="tx")
        elif cmd == "mp_period":  # {"cmd":"mp_period","ms":5}: change the ESP32 poll interval while running (--offload)
            sock = getattr(args, "_sock", None)
            if sock is None or not getattr(args, "_offload", False):
                emit("# gui: mp_period needs --serial with --offload")
            else:
                args.mp_period_ms = float(c["ms"])
                sock.mp_config(True, args.clienttime, args.cmd_word, args.cmdcount, args.mp_period_ms * 1000,
                               args.reply_wait * 1000, args.poll_cycle)
                emit("# gui: ESP32 poll interval now %.2f ms" % args.mp_period_ms)
        elif cmd == "echo":
            emit("# gui: the host always echoes client messages now (echo setting ignored)")
        elif cmd in ("say", "draw"):
            emit("# gui: %s is not available from the command line yet; use chat with pixels" % cmd)
        else:
            emit("# gui: unknown command %r" % (c,))
    except (KeyError, ValueError, TypeError, IndexError, AttributeError) as e:
        # a client must never be able to stop the host: report the command and carry on
        emit("# gui: bad command %s: %s: %s" % (repr(c)[:120], type(e).__name__, e))


def host(args):
    if args.gui_port and args.gui_bind not in ("127.0.0.1", "localhost", "::1") and not args.gui_token:
        sys.exit("--gui-bind %s opens the API to other machines: set a token with --gui-token or the "
                 "PICTOCHAT_TOKEN environment variable" % args.gui_bind)
    src = bytes.fromhex(args.mac.replace(":", ""))
    if len(src) != 6 or src[0] & 1:
        sys.exit("--mac must be a 6-byte unicast address")
    st = HostState(seed=args.seed, room=args.room)
    total = args.count if args.count else None
    if getattr(args, "bridge", 0) and not args.dry_run:
        pass
    if args.dry_run:
        n = total or 100
        with open(args.dry_run, "wb") as fh:
            fh.write(pcap_header(127))
            t = 100.0
            for i in range(n):
                fh.write(pcap_record(t, tx_radiotap(2) + build_beacon(src, args.channel, st, int(t * 1e6))))
                t += st.interval * 1.024e-3
        print("wrote %d beacons to %s" % (n, args.dry_run))
        return
    if args.bridge:
        sock = BridgeSock(args.bridge, args.channel, src)
    elif args.serial:
        sock = SerialSock(args.serial, args.channel, src, forward_all=args.all)
    else:
        if not args.iface:
            sys.exit("--iface is required (or use --bridge PORT for the emulator)")
        if os.geteuid() != 0:
            sys.exit("host needs root: sudo python3 %s host --iface %s ..." % (sys.argv[0], args.iface))
        if not args.no_setup:
            setup_monitor(args.iface, mac(src))
        if not set_channel(args.iface, args.channel):
            sys.exit("could not set channel %d" % args.channel)
        sock = open_sock(args.iface)
    nofiles = args.out in ("", "none")              # a service that runs for months must not grow a log and a pcap
    pcap = open(os.devnull if nofiles else args.out + ".pcap", "wb")
    pcap.write(pcap_header(127))
    logf = open(os.devnull if nofiles else args.out + ".log", "w")
    t0 = time.time()

    cmdq = queue.Queue()
    bridge = None
    if args.gui_port:
        try:
            bridge = GuiBridge(args.gui_port, cmdq, args.gui_bind, args.gui_token, lambda: time.time() - t0)
        except OSError as e:
            sys.exit("cannot listen on %s:%d: %s" % (args.gui_bind, args.gui_port, e))

    def emit(msg):
        line = "%9.3f %s" % (time.time() - t0, msg)
        print(line)
        logf.write(line + "\n")
        logf.flush()
        if bridge:
            bridge.send({"kind": "log", "t": time.time() - t0, "text": msg})

    offload = False
    mp_base = None
    if isinstance(sock, SerialSock):
        sock.emit = emit
        offload = sock.mp and args.offload and not args.no_poll
        if sock.mp:
            # The modem keeps its client list and packet queue across host runs (USB does not reset it). Start clean,
            # or a DS that rejoins under its old AID is not treated as new and never gets the opening poll.
            sock.mp_clients({})
            sock.mp_clear()
        if offload:
            sock.mp_config(True, args.clienttime, args.cmd_word, args.cmdcount, args.mp_period_ms * 1000,
                           args.reply_wait * 1000, args.poll_cycle)
        elif sock.mp:
            sock.mp_config(False, args.clienttime, args.cmd_word, args.cmdcount, 0, 0, 1)
        args._sock, args._offload = sock, offload
        emit("# polling: %s" % ("on the ESP32 every %.1f ms (multiplay offload)" % args.mp_period_ms if offload
                                 else "from this program every %.1f ms" % args.poll_ms))
    commit = build_commit()
    radio = ("emulator bridge" if args.bridge else
             "ESP32-C3 modem, polls on the ESP32" if offload else
             "ESP32-C3 modem, polls from the host" if args.serial else "Wi-Fi adapter %s" % args.iface)
    emit("# pictochat.py %s%s, radio: %s" % (__version__, " (commit %s)" % commit if commit else "", radio))

    def gui(kind, **kw):
        if bridge:
            kw.update(kind=kind, t=time.time() - t0)
            bridge.send(kw)

    markers = []

    def stdin_thread():
        for line in sys.stdin:
            if line.strip():
                markers.append(line.strip())

    threading.Thread(target=stdin_thread, daemon=True).start()

    emit("# beaconing as %s on channel %d, room index %d (0=A). Logging to %s.log / .pcap. "
         "Type a label + Enter for a marker, Ctrl-C to stop." % (mac(src), args.channel, args.room, args.out))
    tr = Tracker()
    stations = {}            # DS MAC -> frame count
    rssi = {}                # DS MAC -> [last dBm, smoothed dBm, time heard, frames]  (reported in the state event)
    reply_t = [time.time(), -1]   # [time the reply counter last changed, its value]
    answered = set()
    aids = {}                # DS MAC -> AID (1..15)
    mp = Counter()
    seen_mp = set()
    next_poll = None
    ack_deadline = None
    ack_fail = 0
    cmd_cycle = -1
    cmd_first = False
    next_keepalive = 0.0
    ack_mask_poll = False
    cmd_carried = False
    last_replied = 0
    hp_value = hp_key = 0
    hp_frame = 0
    ml = MultiplayHost(src, args.name, args.color, st._step, args.hello_repeats, lambda m: emit("# " + m))
    rx_seen = {}
    frag_seen = {}
    assem = {}
    stats = Counter()
    sent = 0
    next_t = t0
    next_status = t0 + 5

    def on_sigterm(*_):                 # systemd stop / kill: leave through the same clean-up as Ctrl-C
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, on_sigterm)
    try:
        while total is None or sent < total:
            now = time.time()
            while markers:
                emit("---- MARKER: " + markers.pop(0))
            while not cmdq.empty():
                handle_gui_cmd(cmdq.get(), args, ml, src, emit, gui)
            # offload: hand queued packets to the ESP32, which repeats each one on its polls until confirmed
            while offload and ml.queue and sock.mp_outstanding() < MP_OUTSTANDING:
                port_, pl_, reps_, _ann, min_sent_ = ml.queue.pop(0)[:5]
                sock.mp_push(port_, pl_, min_sent_ if args.confirm else reps_, reps_)
                gui("tx", port=port_, ptype=struct.unpack_from("<H", pl_, 0)[0] if len(pl_) >= 2 else -1,
                    hex=pl_.hex(), repeats=0)
            if now >= next_t:
                try:
                    sock.send(tx_radiotap(2) + build_beacon(src, args.channel, st, st.tsf))
                    sent += 1
                    next_t += st.interval * 1.024e-3
                    if next_t < now - 1:
                        next_t = now
                except BlockingIOError:
                    pass
                except OSError as e:
                    emit("# send error: %s" % e)
                    time.sleep(0.5)
            if aids and not args.no_poll and not offload and next_poll is None:
                next_poll = now
            if not aids:
                next_poll = None
                ack_deadline = None
            lockstep = getattr(sock, "want_poll", None) is not None and args.lockstep
            if lockstep and hasattr(sock, "_fill"):
                try:
                    sock._fill()
                except OSError:
                    pass
            poll_due = (sock.want_poll and next_poll is not None) if lockstep else (next_poll is not None and now >= next_poll)
            if poll_due and ack_deadline is None:
                if lockstep:
                    sock.want_poll = False
                full_mask = 0
                for a in aids.values():
                    full_mask |= 1 << a
                if cmd_first:
                    mask = 0                                    # the opening empty poll does not ask for a reply
                else:
                    joined_any = any(c_["stage"] == "joined" for c_ in ml.clients.values())
                    cycle = args.poll_cycle_joined if joined_any else args.poll_cycle
                    cmd_cycle = (cmd_cycle + 1) % cycle
                    if ml.queue and not ml.queue[0][3]:
                        cmd_cycle = 0                          # a new packet always starts on a poll addressed to the clients
                    mask = full_mask if cmd_cycle == 0 else 0  # every poll during the join, every 5th afterwards
                _d, mseq = st.next_mgmt()
                word, data = args.cmd_word, b""
                cmd_first_sent_now = False
                keepalive = not cmd_first and now >= next_keepalive
                if keepalive:
                    next_keepalive = now + 1.0
                    mask = full_mask
                if keepalive:
                    pass                                        # once a second: empty poll, clienttime 0xD6 (real host)
                elif not args.no_hostpkt:
                    port_, pl_, first_, ctr_ = ml.next_entry()
                    word, data = host_word_packet(port_, pl_)
                    if cmd_first:
                        cmd_first_sent_now = True
                        word, data = 0x0000, b""                # the real host opens with an empty poll (no data, no tail)
                        ml.undo_entry(port_)
                        cmd_first = False
                    else:
                        data += struct.pack("<HH", ctr_, mask)   # tail: per-port counter, clients this poll is addressed to
                    if first_:
                        gui("tx", port=port_, ptype=struct.unpack_from("<H", pl_, 0)[0], hex=pl_.hex(), repeats=0)
                try:
                    sock.send(tx_radiotap(2) + build_cmd(src, mseq, 0x00D6 if keepalive else args.clienttime, mask, word, data))
                    mp["cmd"] += 1
                    ack_fail = mask
                    ack_mask_poll = bool(mask)
                    # unaddressed polls get no answer: in lockstep ACK them at once so emulated time keeps running
                    ack_deadline = time.time() + (args.reply_wait / 1000.0 if mask else 0.0)   # unaddressed polls get no reply
                    cmd_carried = not keepalive and not cmd_first_sent_now
                except OSError as e:
                    emit("# cmd send error: %s" % e)
                next_poll = now + args.poll_ms / 1000.0
            if ack_deadline is not None and time.time() >= ack_deadline:
                _d, mseq = st.next_mgmt()
                try:
                    sock.send(tx_radiotap(2) + build_ack(src, mseq, args.cmdcount, ack_fail))
                    mp["ack"] += 1
                except OSError as e:
                    emit("# ack send error: %s" % e)
                if ack_mask_poll:
                    last_replied = full_mask & ~ack_fail & 0xFFFF
                    if ack_fail == 0 and cmd_carried and args.confirm:
                        ml.confirm()                            # every addressed client got that packet
                ack_deadline = None
            if offload and sock.mpstats:
                if mp_base is None:             # the modem counts since its power-up: show this run only
                    mp_base = dict(sock.mpstats)
                mp["cmd"] = max(0, sock.mpstats["polls"] - mp_base["polls"])
                mp["ack"] = max(0, sock.mpstats["acks"] - mp_base["acks"])
                mp["reply"] = max(0, sock.mpstats["replies"] - mp_base["replies"])
            if now >= next_status:
                emit("# status: %d beacons sent, counter=%d, interval=%dTU, stations heard: %s, frames logged: %d, "
                     "aids: %s, CMD sent %d, replies heard %d, ACK sent %d"
                     % (sent, st.counter, st.interval, ", ".join("%s(%d)" % (mac(k), v) for k, v in stations.items()) or "none",
                        stats["logged"], ", ".join("%s=%d" % (mac(k), v) for k, v in aids.items()) or "none",
                        mp["cmd"], mp["reply"], mp["ack"]))
                next_status = now + 5
                if mp["reply"] != reply_t[1]:
                    reply_t[:] = [now, mp["reply"]]
                gui("state", version=__version__, commit=commit, impl="python", radio=radio,
                    uptime=round(now - t0), aids={mac(k): v for k, v in aids.items()}, cmd=mp["cmd"],
                    replies=mp["reply"], beacons=sent,
                    echo=bool(args.echo), cycle=args.poll_cycle_joined, reply_age=round(now - reply_t[0], 1),
                    rssi={mac(k): [v[0], round(v[1], 1), round(now - v[2], 1), v[3]] for k, v in rssi.items()})
            r = [sock] if getattr(sock, 'pending', None) else select.select([sock], [], [], max(0.0, min(0.02, next_t - time.time())))[0]
            if not r:
                continue
            try:
                pkt, _ = sock.recvfrom(4096)
            except (BlockingIOError, OSError):
                continue
            ts = time.time()
            f = parse_frame(ts, pkt, 127)
            if f is None:
                continue
            stats["seen"] += 1
            if f.a2 and bytes(f.a2) == src and (f.cls == "beacon" or f.cls in ("MP-CMD", "MP-ACK")):
                if f.cls != "beacon":
                    pcap.write(pcap_record(ts, pkt))   # our own polls/ACKs: keep in the pcap, but no costly text log
                continue    # our own transmission looped back
            if f.signal is not None and f.a2 and bytes(f.a2) in aids:   # signal strength of each joined DS
                r_ = rssi.get(bytes(f.a2))
                if r_ is None:
                    rssi[bytes(f.a2)] = [f.signal, float(f.signal), ts, 1]
                else:
                    r_[0], r_[1], r_[2], r_[3] = f.signal, r_[1] + 0.2 * (f.signal - r_[1]), ts, r_[3] + 1
            if f.cls == "beacon" and not args.all:
                if not (has_nintendo_ie(f) and f.a2 and bytes(f.a2) != src):
                    continue
            tr.learn(f)
            mine = bool((f.a1 and bytes(f.a1) == src) or (f.a2 and bytes(f.a2) == src))
            from_nin = bool(f.a2 and bytes(f.a2)[:3] in NINTENDO_OUIS)
            if not (args.all or mine or from_nin or tr.relevant(f)):
                continue
            if f.cls == "MP-REPLY":
                if not offload:
                    mp["reply"] += 1
                ent = wm_entry(f.body)
                if ent and ent[1] and f.a2 and bytes(f.a2) in aids:
                    port, pl = ent
                    ptype = struct.unpack_from("<H", pl, 0)[0] if len(pl) >= 2 else -1
                    mp["port%x_type%d" % (port, ptype)] += 1
                    if mp["port%x_type%d" % (port, ptype)] == 1:
                        emit("# DS packet: port 0x%x type %d len %d payload=%s" % (port, ptype, len(pl), pl[:24].hex()))
                    nowt = time.time()
                    if nowt - rx_seen.get((port, pl), 0) > 0.5:
                        rx_seen[(port, pl)] = nowt
                        gui("rx", port=port, ptype=ptype, hex=pl.hex(), mac=mac(f.a2))
                    if not args.no_hostpkt:
                        nmsg = len(ml.messages)
                        ml.on_reply(aids[bytes(f.a2)], port, pl, nowt)
                        for mid_, msg_ in ml.messages[nmsg:]:
                            emit("# DS message complete: id %d, %d bytes: %s" % (mid_, len(msg_), msg_[:48].hex()))
                            gui("message", id=mid_, hex=msg_.hex(), dir="rx")
                if ack_deadline is not None:
                    if f.a2 and bytes(f.a2) in aids:
                        ack_fail &= ~(1 << aids[bytes(f.a2)]) & 0xFFFF   # that client answered
                    ack_deadline = time.time()    # reply heard: ACK right away
            if f.cls == "deauth" and f.a2 and bytes(f.a2) in aids:
                emit("# station %s deauthenticated (AID %d freed), reason %s" % (
                    mac(f.a2), aids[bytes(f.a2)], f.body[:2].hex()))
                ml.on_deauth(aids[bytes(f.a2)])
                del aids[bytes(f.a2)]
                if offload:
                    if not aids:
                        sock.mp_clear()
                    sock.mp_clients(aids)
                st.members = 1 + len(aids)
                st.on_members_changed()
            if f.cls.startswith("MP-") or (f.ftype == 2 and f.a1 and bytes(f.a1)[:3] == MP_PREFIX):
                key = (f.cls, bytes(f.body[2:] if f.cls == "MP-CMD" else f.body).hex())
                if key in seen_mp and mp["logged_" + f.cls] >= 3:
                    pcap.write(pcap_record(ts, pkt))
                    continue    # throttle: log only new payloads (still saved to the pcap)
                seen_mp.add(key)
                mp["logged_" + f.cls] += 1
            if from_nin or bytes(f.a2 or b"") in tr.ds:
                m = bytes(f.a2)
                if m not in stations:
                    emit("# NEW STATION %s (first frame: %s)" % (mac(m), f.cls))
                stations[m] = stations.get(m, 0) + 1
            stats["logged"] += 1
            stats[f.cls] += 1
            tr.add(f)
            pcap.write(pcap_record(ts, pkt))
            pcap.flush()
            text = describe(f, t0)
            if f.ftype == 0 and f.body and f.cls != "beacon":
                text += "\n        full body (%dB): %s" % (len(f.body), f.body.hex())
            emit(text)
            if f.a1 and bytes(f.a1) == src and f.a2 and not args.no_reply:
                aid = 1
                if f.cls == "assoc-req":
                    m_ = bytes(f.a2)
                    if m_ not in aids:
                        free = [a for a in range(1, 16) if a not in aids.values()]
                        if free:
                            aids[m_] = free[0]
                    aid = aids.get(m_, 1)
                    emit("# assoc-req from %s -> AID %d" % (mac(m_), aid))
                    if m_ in aids:
                        ml.on_assoc(aids[m_], m_)
                        if offload:
                            sock.mp_clear()                     # on_assoc dropped the queue: drop the modem's too
                            sock.mp_clients(aids, reopen=True)
                        st.members = 1 + len(aids)
                        st.status = 0xBD8A
                        st.on_members_changed()
                        cmd_first = True
                        next_keepalive = time.time() + 1.0
                        last_replied = 0
                        cmd_cycle = -1
                reply = host_reply(src, f, st, aid)
                key = (bytes(f.a2), f.cls, f.seq)
                if reply and key not in answered:
                    answered.add(key)
                    try:
                        sock.send(tx_radiotap(2) + reply)
                        emit("# -> replied to %s (seq %d) with %s" % (f.cls, f.seq, reply[24:].hex()))
                    except OSError as e:
                        emit("# reply send error: %s" % e)
    except KeyboardInterrupt:
        pass
    finally:
        if isinstance(sock, SerialSock) and sock.mp:
            try:                        # leave the modem idle: no clients, no polling
                sock.mp_clients({})
                sock.mp_clear()
                sock.mp_config(False, args.clienttime, args.cmd_word, args.cmdcount, 0, 0, 1)
            except Exception:
                pass
        emit("# sent %d beacons, saw %d frames, logged %d" % (sent, stats["seen"], stats["logged"]))
        emit("# logged by class: " + (", ".join("%s=%d" % (k, v) for k, v in sorted(stats.items()) if k not in ("seen", "logged")) or "none"))
        emit(tr.summary())
        logf.close()
        pcap.close()
        print("\nwrote %s.pcap and %s.log" % (args.out, args.out))


# ---------------------------------------------------------------------------
# self test with synthetic frames

def _radiotap(tsft, rate_mbps, freq, sig):
    # present: TSFT(0) FLAGS(1) RATE(2) CHANNEL(3) DBM_ANTSIGNAL(5)
    present = (1 << 0) | (1 << 1) | (1 << 2) | (1 << 3) | (1 << 5)
    body = struct.pack("<Q", tsft) + bytes([0, int(rate_mbps * 2)]) + struct.pack("<HH", freq, 0x80) + struct.pack("<b", sig)
    hdr = struct.pack("<BBHI", 0, 0, 8 + len(body), present)
    return hdr + body


def _dot11(fc0, fc1, a1, a2, a3, seq, body=b""):
    return bytes([fc0, fc1]) + b"\x00\x00" + a1 + a2 + a3 + struct.pack("<H", seq << 4) + body


def selftest(_args=None):
    ds = bytes.fromhex("0016564e0001")
    host_ie = (b"\xdd" + bytes([8 + 6]) + NINTENDO_OUI + b"\x00" + struct.pack("<HH", 0xFFFF, 0x1234) + bytes.fromhex("0a0b0c0d0e0f"))
    beacon_body = (b"\x00" * 8 + struct.pack("<HH", 100, 0x0021) + b"\x01\x04\x82\x84\x8b\x96" +
                   b"\x03\x01\x07" + b"\x05\x05\x00\x01\x00\x00\x00" + host_ie)
    frames = []
    t = 1_000_000
    frames.append(_radiotap(t, 1, 2442, -40) + _dot11(0x80, 0, BROADCAST, ds, ds, 5, beacon_body))
    for i in range(4):
        t += 20000
        frames.append(_radiotap(t, 2, 2442, -41) +
                      _dot11(0x28, 0x02, MP_CMD, ds, ds, 100 + 2 * i, bytes.fromhex("0000050000340000") + bytes(20)))
        t += 300
        frames.append(_radiotap(t, 2, 2442, -42) +
                      _dot11(0x18, 0x01, ds, bytes.fromhex("0016565e0002"), MP_REPLY, 7 + i, bytes.fromhex("0000010097") + bytes(10)))
        t += 120
        frames.append(_radiotap(t, 2, 2442, -41) + _dot11(0x18, 0x02, MP_ACK, ds, ds, 101 + 2 * i, bytes(8)))
    path = "/tmp/pictochat_selftest.pcap"
    with open(path, "wb") as fh:
        fh.write(pcap_header(127))
        for i, p in enumerate(frames):
            fh.write(pcap_record(100.0 + i * 0.01, p))
    frames_dec = [parse_frame(ts, pkt, lt) for ts, pkt, lt in read_pcap(path)]
    tr = analyze_stream(frames_dec)
    print(tr.summary())
    classes = [e[2] for e in tr.events]
    assert classes[0] == "beacon" and classes.count("MP-CMD") == 4, classes
    assert classes.count("MP-REPLY") == 4 and classes.count("MP-ACK") == 4, classes
    assert mac(ds) in tr.summary()
    f0 = frames_dec[0]
    assert freq_to_ch(f0.freq) == 7 and f0.rate == 1.0 and f0.signal == -40
    assert "fieldB=0x1234" in describe_beacon(f0)
    # host beacon builder
    assert (lambda x: x)(0x6c01) == _lcg4(0x22dd), "LCG does not reproduce the captured nonce sequence"
    src = bytes.fromhex("001b7a000001")
    st = HostState(seed=1234, room=0)
    bpath = "/tmp/pictochat_selftest_host.pcap"
    with open(bpath, "wb") as fh:
        fh.write(pcap_header(127))
        for i in range(60):
            fh.write(pcap_record(200.0 + i * 0.1, _radiotap(0, 2, 2412, -30) + build_beacon(src, 1, st)))
    hb = [parse_frame(ts, pkt, lt) for ts, pkt, lt in read_pcap(bpath)]
    assert all(has_nintendo_ie(f) for f in hb)
    gis = [d[8:] for f in hb for t, d, _ in parse_ies(f.body[12:]) if t == 0xDD]
    assert all(len(g) == 24 and g[:8] == bytes([1, 0, 0, 0, 0, 0, 0, 0]) and g[10:18] == bytes.fromhex("0801c000c0004823") and g[20:] == bytes.fromhex("00010400") for g in gis)
    ctr = [struct.unpack_from("<H", g, 8)[0] for g in gis]
    non = [struct.unpack_from("<H", g, 18)[0] for g in gis]
    assert ctr[0] == 1 and ctr[24] == 1 and ctr[25] == 2 and ctr[50] == 3, ctr
    assert non[25] == _lcg4(non[0]) and non[50] == _lcg4(non[25])
    assert [f.seq for f in hb[:3]] == [0, 1, 2]
    print("host beacon OK:", describe_beacon(hb[0]).splitlines()[0])
    print("\nselftest OK")


# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("capture", help="live capture (root)")
    c.add_argument("--iface", required=True)
    c.add_argument("--out", default="pictochat_capture")
    c.add_argument("--channel", type=int, help="fixed channel instead of hopping")
    c.add_argument("--channels", help="comma list to hop over, e.g. 1,7,13 (fewer channel changes is gentler on the USB adapter)")
    c.add_argument("--dwell", type=float, default=0.6, help="seconds per channel while hopping")
    c.add_argument("--no-setup", action="store_true", help="interface is already in monitor mode")
    c.add_argument("--no-lock", action="store_true", help="keep hopping even after DS traffic is found")
    c.add_argument("--all", action="store_true", help="record every frame, not just DS-related ones")
    c.set_defaults(fn=capture)
    a = sub.add_parser("analyze", help="decode a pcap")
    a.add_argument("pcap")
    a.add_argument("--all", action="store_true")
    a.set_defaults(fn=analyze)
    h = sub.add_parser("host", help="beacon as a fake PictoChat room (root)")
    h.add_argument("--iface", help="monitor-capable interface (not needed with --dry-run)")
    h.add_argument("--channel", type=int, default=1)
    h.add_argument("--mac", default="00:1b:7a:00:00:01", help="BSSID / source MAC")
    h.add_argument("--room", type=int, default=0, help="room index for the beacon: 0-3 = Chat Room A-D (game-info byte 20; the channel is separate: A was channel 1, B channel 7 in the emulator)")
    h.add_argument("--seed", type=int, help="LCG seed (default random)")
    h.add_argument("--count", type=int, help="stop after N beacons")
    h.add_argument("--dry-run", metavar="PCAP", help="write the beacons to a pcap instead of transmitting")
    h.add_argument("--no-setup", action="store_true")
    h.add_argument("--out", default="host_log", help="log/pcap basename (default host_log); 'none' writes no files")
    h.add_argument("--all", action="store_true", help="log every frame heard, not just DS-related ones")
    h.add_argument("--mp-period-ms", type=float, default=7.0, help="with --offload: interval between polls on the ESP32. Real DS, 15 s per step: 20 ms 92%% answered, 10 ms 96%%, 7 ms 91%%, 5 ms 79%%; a full drawing took 2.2 s at 7 ms (5.8 s with polling from Python). 2.5 ms: the DS stopped answering and a Pi next to the ESP32 lost Wi-Fi")
    h.add_argument("--offload", action="store_true", help="with --serial and the offload firmware: run the poll / reply / ACK loop on the ESP32 (see --mp-period-ms)")
    h.add_argument("--poll-ms", type=float, default=5.0, help="interval between CMD polls once a DS is associated (real host: ~2.2 ms; 20 ms worked but made messages slow)")
    h.add_argument("--clienttime", type=lambda x: int(x, 0), default=0x3E6, help="CMD clienttime (reply window per client, us); 0xD0 + 4*max reply bytes")
    h.add_argument("--cmd-word", type=lambda x: int(x, 0), default=0, help="third CMD body word (meaning unknown)")
    h.add_argument("--cmdcount", type=lambda x: int(x, 0), default=0x54, help="value in the ACK's first word (run-ahead; meaning partly unknown)")
    h.add_argument("--no-hostpkt", action="store_true", help="send empty polls (no PictoChat-layer packet in the CMD data)")
    h.add_argument("--lockstep", action="store_true", help="TEST (--bridge only): poll whenever the emulated client waits for a poll instead of on a wall-clock timer")
    h.add_argument("--serial", metavar="TTY", help="use the ESP32-C3 radio modem (esp32c3_modem/) on this serial "
                   "port, e.g. /dev/ttyACM0, instead of a monitor-mode adapter (no root needed)")
    h.add_argument("--bridge", type=int, default=0, help="TEST: connect to the melonDS harness on 127.0.0.1:PORT instead of a radio")
    h.add_argument("--gui-port", type=int, default=0, help="listen on 127.0.0.1:PORT for pictochat_gui.py, bots and Home Assistant (0 = off)")
    h.add_argument("--gui-bind", default="127.0.0.1", metavar="ADDR", help="address the API port listens on; 0.0.0.0 lets other machines (Home Assistant) connect and requires a token")
    h.add_argument("--gui-token", default=os.environ.get("PICTOCHAT_TOKEN", ""), metavar="TOKEN", help="shared secret a client must send first ({\"cmd\":\"auth\",\"token\":...}); default: $PICTOCHAT_TOKEN")
    h.add_argument("--echo", action="store_true", help="(obsolete, ignored) EXPERIMENT: echo the DS's port 0xE message fragments back (the first real run froze/crashed the DS's PictoChat)")
    h.add_argument("--announce", action="store_true", help="(obsolete, ignored) EXPERIMENT: after the port 0xD handshake send a host join message (id 0, same layout as the DS's own) on port 0xE")
    h.add_argument("--name", default="py", help="host nickname shown in the room (UTF-16, max 10 chars)")
    h.add_argument("--color", type=int, default=2, help="host favourite colour index (0-15) in its member message")
    h.add_argument("--poll-cycle-joined", type=int, default=1, help="client mask on every Nth poll once a client has joined. The real host uses 5; on the radio 1 (every poll) lets every copy of a packet reach the DS and every reply confirm delivery")
    h.add_argument("--reply-wait", type=float, default=4.0, help="ms to wait for the DS's reply to an addressed poll before sending the ACK")
    h.add_argument("--no-confirm", dest="confirm", action="store_false", help="always send every packet --hello-repeats times instead of moving on once the DS has answered a poll carrying it")
    h.add_argument("--poll-cycle", type=int, default=1, help="set the client mask on every Nth poll. The real host uses 5, but 1 gave the emulated client a working join (hello, token request, member message) and is what the real DS answered earlier")
    h.add_argument("--hello-repeats", type=int, default=8, help="maximum number of polls that carry each packet (fewer once the DS confirms it, see --no-confirm)")
    h.add_argument("--pkt-type", type=lambda x: int(x, 0), default=4, help="(obsolete, ignored) type field of the host packet (4 = host info, the answer to the client's type 6)")
    h.add_argument("--pkt-key", type=lambda x: int(x, 0), default=0, help="(obsolete, ignored) optional u32 written at body offset 16 (inside the 0x60 info bytes)")
    h.add_argument("--pkt-len", type=int, default=108, help="(obsolete, ignored) body length of the experimental host packet")
    h.add_argument("--no-poll", action="store_true", help="associate but do not send CMD/ACK frames")
    h.add_argument("--no-reply", action="store_true", help="only beacon, do not answer auth/assoc requests")
    h.set_defaults(fn=host)
    s = sub.add_parser("selftest", help="check the decoder on synthetic frames")
    s.set_defaults(fn=selftest)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
