#!/usr/bin/env python3
"""
pictochat_gui.py - window for the fake PictoChat host: shows what the DS sends, lets you send chat messages.

Run the host with the GUI port, then this window as your normal user:

    sudo python3 pictochat.py host --iface <wifi-interface> --channel 1 --gui-port 7777
    python3 pictochat_gui.py                      # connects to 127.0.0.1:7777

A PictoChat message is a picture (256 x 80 pixels, see PROTOCOL.md section 7). So:
  * draw with the mouse on the canvas and press "Send drawing", or
  * type text and press "Send text" (needs Pillow: it renders the text into the picture), or
  * press "Load image..." to dither a picture file onto the canvas (Floyd-Steinberg, or threshold; "colour" maps
    it to PictoChat's 16-colour palette), then send it. The pen row picks the ink colour for drawing and text.
Messages are held until a DS has finished joining the room and are then sent from the host.

Left: every packet from the DS (rx) and every packet we queue for it (tx); yellow rows are complete messages.
Right: details of the selected row; drawn chat messages are shown as the picture they contain.

Commands (command line at the bottom; also Enter):
    say <text>                   render the text into the canvas and send it
    draw                         send the canvas as a chat message
    clear                        clear the canvas
    pen x y | line x1 y1 x2 y2   draw on the canvas (0-255, 0-79)
    announce [name]              send the host's own member message again
    raw <port> <hex> [repeats]   send a raw payload on a WM port (0xC, 0xD, 0xE)
    image <file>                 load an image, dither it onto the canvas (then: draw)
    save <file.ppm>              write the canvas as a PPM image
    clear-log                    clear the lists
"""
import argparse
import json
import os
import queue
import socket
import struct
import threading
import time
import tkinter as tk
from tkinter import ttk

PW, PH = 256, 80                  # size of a PictoChat message picture
SCALE = 2
# PictoChat's 16-colour palette as shown by the DS (measured on the emulated DS Lite): 0 = paper, 1 = black ink,
# 2 = light grey, 3-15 = the rainbow pen colours from magenta through red, yellow, green and cyan to blue.
PALETTE = {0: (251, 251, 251), 1: (0, 0, 0), 2: (211, 203, 195), 3: (235, 0, 235), 4: (251, 0, 138),
           5: (251, 0, 40), 6: (251, 73, 0), 7: (251, 162, 0), 8: (227, 243, 0), 9: (130, 251, 0),
           10: (16, 251, 32), 11: (0, 251, 186), 12: (0, 195, 251), 13: (0, 121, 251), 14: (0, 48, 251),
           15: (40, 0, 251)}
# what the DS shows of the picture (measured on the emulated DS): columns 23-252; the sender's name tab covers
# columns 0-80 of rows 0-15. Ink outside is invisible or drawn over the bubble frame, so it is clipped before sending.
VIS_X0, VIS_X1, TAB_X1, TAB_Y1 = 23, 252, 80, 15


def visible(x, y):
    return VIS_X0 <= x <= VIS_X1 and not (x <= TAB_X1 and y <= TAB_Y1)


def clip(rows):
    return [[v if visible(x, y) else 0 for x, v in enumerate(r)] for y, r in enumerate(rows)]


# ---------------------------------------------------------------- decoding helpers
def hexdump(b, width=16):
    out = []
    for i in range(0, len(b), width):
        chunk = b[i:i + width]
        out.append("%04x  %-*s  %s" % (i, width * 3, " ".join("%02x" % x for x in chunk),
                                       "".join(chr(x) if 32 <= x < 127 else "." for x in chunk)))
    return "\n".join(out)


def utf16_guess(b):
    """Longest run of plain UTF-16LE text (ASCII, Latin-1, kana), at least 2 characters. Kanji are left out on
    purpose: byte pairs such as MACs look like kanji and would give false hits."""
    best = ""
    for start in (0, 1):
        cur = ""
        for i in range(start, len(b) - 1, 2):
            ch = b[i] | (b[i + 1] << 8)
            if 32 <= ch < 0x7F or 0xA0 <= ch < 0x180 or 0x3040 <= ch <= 0x30FF:
                cur += chr(ch)
            else:
                if len(cur) > len(best):
                    best = cur
                cur = ""
        if len(cur) > len(best):
            best = cur
    return best if len(best) >= 2 else ""


def swap16(b):
    return bytes(b[i ^ 1] for i in range(len(b)))


def mac_str(b):
    return ":".join("%02x" % x for x in b)


def swapped_macs(b):
    """MACs stored with every 16-bit pair byte-swapped (as in the DS's messages and roster)."""
    out = []
    for i in range(0, len(b) - 5):
        m = swap16(b[i:i + 6])
        if m[:3] in (b"\x00\x1b\x7a", b"\x00\x09\xbf", b"\x00\x16\x56", b"\x00\x17\xab", b"\x00\x19\x1d", b"\x00\x1a\xe9"):
            out.append((i, mac_str(m)))
    return out


def member_text(msg):
    """Description of a `03 00` / `03 01` member message, or None."""
    if len(msg) >= 28 and msg[0] == 3 and msg[1] in (0, 1):
        name = msg[8:28].decode("utf-16-le", "replace").split("\x00")[0]
        color = struct.unpack_from("<H", msg, len(msg) - 4)[0] if len(msg) >= 4 else 0
        return "%s: %r (%s) colour %d" % ("joining" if msg[1] == 0 else "ready", name, mac_str(swap16(msg[2:8])), color)
    return None


def decode_image(msg):
    """Pixels (80 rows of 256 palette indices) of a `03 02` drawn chat message, else None."""
    if len(msg) < 36 + 2048 or msg[0] != 3 or msg[1] != 2:
        return None
    data = msg[36:36 + 10240]                  # 20-byte header, 16 zero bytes, then 1-5 lines of 2048 bytes
    rows = [[0] * PW for _ in range(PH)]
    for t in range(len(data) // 32):
        tx, ty = t % 32, t // 32
        for y in range(8):
            for x in range(8):
                b = data[t * 32 + y * 4 + x // 2]
                rows[ty * 8 + y][tx * 8 + x] = (b >> 4) if x & 1 else (b & 15)
    return rows


def pixels_to_ppm(rows, shade=False):
    """PPM of the picture; shade=True greys the parts the DS does not show (for the drawing canvas)."""
    out = bytearray(b"P6\n%d %d\n255\n" % (len(rows[0]), len(rows)))
    for y, r in enumerate(rows):
        for x, v in enumerate(r):
            if v:
                out += bytes(PALETTE.get(v, (128, 128, 128)))
            elif shade and not visible(x, y):
                out += b"\xc8\xc8\xc8"
            else:
                out += b"\xff\xff\xff"
    return bytes(out)


TEXT_X0, TEXT_X1, TEXT_LINES = 26, 250, 4      # text area: lines 2-5 of the picture, inside the visible columns


def _font():
    from PIL import ImageFont
    for name in ("DejaVuSans.ttf", "FreeSans.ttf", "LiberationSans-Regular.ttf"):
        try:
            return ImageFont.truetype(name, 13)
        except OSError:
            continue
    return ImageFont.load_default()


def wrap_text(text):
    """Word-wrap text to the width of the visible picture area. Returns the list of lines (any number; one picture
    holds TEXT_LINES of them). Needs Pillow (raises ImportError)."""
    from PIL import Image, ImageDraw
    font = _font()
    d = ImageDraw.Draw(Image.new("L", (1, 1)))
    lines = []
    for para in text.split("\n"):
        cur = ""
        for word in para.split(" "):
            trial = (cur + " " + word).strip()
            if d.textlength(trial, font=font) <= TEXT_X1 - TEXT_X0 or not cur:
                cur = trial
            else:
                lines.append(cur)
                cur = word
        lines.append(cur)
    return lines


def text_to_pixels(text, color=1):
    """Render text into the 80 x 256 picture with Pillow (None without Pillow). The DS shows picture columns ~22-252
    and its name tab covers the top-left of the first 16-px line, so text goes on lines 2-5 (16 px each), x >= 26.
    Text beyond 4 wrapped lines is cut off (see wrap_text to split it into several pictures)."""
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return None
    font = _font()
    img = Image.new("L", (PW, PH), 0)
    d = ImageDraw.Draw(img)
    for i, ln in enumerate(wrap_text(text)[:TEXT_LINES]):
        d.text((TEXT_X0, 17 + 16 * i), ln, fill=255, font=font)
    px = img.load()
    return [[color if px[x, yy] > 110 else 0 for x in range(PW)] for yy in range(PH)]


def image_to_pixels(path, dither=True, invert=False, color=False):
    """Load an image file (any format Pillow reads), fit it into the part of the picture the DS shows below the name
    tab (columns 23-252, rows 16-79: 230 x 64 px, aspect ratio kept, centred) and convert it to 1-bit ink with
    Floyd-Steinberg dithering (or a plain threshold). With color=True the image is mapped to PictoChat's 16-colour
    palette (PALETTE) instead of black and white. Returns 80 rows of 256 palette indices, or raises ImportError."""
    from PIL import Image, ImageOps
    img = Image.open(path)
    img = ImageOps.exif_transpose(img).convert("RGBA")
    bg = Image.new("RGBA", img.size, (255, 255, 255, 255))      # transparent parts become white
    img = Image.alpha_composite(bg, img).convert("RGB")
    bw, bh = VIS_X1 - VIS_X0 + 1, PH - (TAB_Y1 + 1)
    if invert:
        img = ImageOps.invert(img)
    if color:
        img.thumbnail((bw, bh), Image.LANCZOS)
        pal = Image.new("P", (1, 1))
        flat = []
        for i in range(16):
            flat += list(PALETTE[i])
        pal.putpalette(flat + flat[-3:] * (256 - 16))           # pad with the last colour, never chosen twice
        q8 = img.quantize(palette=pal, dither=Image.FLOYDSTEINBERG if dither else Image.NONE)
        ox = VIS_X0 + (bw - q8.width) // 2
        oy = TAB_Y1 + 1 + (bh - q8.height) // 2
        rows = blank()
        px = q8.load()
        for y in range(q8.height):
            for x in range(q8.width):
                rows[oy + y][ox + x] = min(px[x, y], 15)
        return rows
    img = ImageOps.autocontrast(img.convert("L"), cutoff=1)
    img.thumbnail((bw, bh), Image.LANCZOS)
    one = img.convert("1", dither=Image.FLOYDSTEINBERG if dither else Image.NONE)
    ox = VIS_X0 + (bw - one.width) // 2
    oy = TAB_Y1 + 1 + (bh - one.height) // 2
    rows = blank()
    px = one.load()
    for y in range(one.height):
        for x in range(one.width):
            if px[x, y] == 0:                                     # black = ink
                rows[oy + y][ox + x] = 1
    return rows


def blank():
    return [[0] * PW for _ in range(PH)]


def draw_line(rows, x0, y0, x1, y1, width=2, color=1):
    """Bresenham line with a small square pen into the picture."""
    dx, dy = abs(x1 - x0), -abs(y1 - y0)
    sx, sy = (1 if x0 < x1 else -1), (1 if y0 < y1 else -1)
    err = dx + dy
    while True:
        for ox in range(width):
            for oy in range(width):
                x, y = x0 + ox, y0 + oy
                if 0 <= x < PW and 0 <= y < PH:
                    rows[y][x] = color
        if x0 == x1 and y0 == y1:
            break
        e2 = 2 * err
        if e2 >= dy:
            err += dy
            x0 += sx
        if e2 <= dx:
            err += dx
            y0 += sy


def summarize(kind, port, ptype, data):
    """One-line summary for a row."""
    if kind.startswith("message"):
        mt = member_text(data)
        if mt:
            return "message: member " + mt
        if len(data) >= 2 and data[0] == 3 and data[1] == 2:
            return "message: drawn chat message from %s (%d bytes)" % (mac_str(swap16(data[2:8])), len(data))
        s = utf16_guess(data)
        return "message %d bytes%s" % (len(data), (" text %r" % s) if s else "")
    names = {(0xC, 4): "host info", (0xC, 5): "roster", (0xC, 6): "client hello", (0xD, 0): "token request",
             (0xD, 1): "token grant", (0xD, 3): "ack", (0xE, 2): "message fragment"}
    name = names.get((port, ptype), "?")
    if port == 0xE and ptype == 2 and len(data) >= 12:
        _t, _l, mid, _x, flen, last, off = struct.unpack_from("<HHBBBBI", data, 0)
        name += " (sender id %d, offset %d, %d bytes, last=%d)" % (mid, off, flen, last)
    if port == 0xD and len(data) >= 20:
        name += " id %d random %08x" % (data[4], struct.unpack_from("<I", data, 16)[0])
    return "port 0x%x type %d: %s" % (port, ptype, name)


# ---------------------------------------------------------------- the window
class App:
    def __init__(self, root, host, port):
        self.root = root
        self.addr = (host, port)
        self.q = queue.Queue()
        self.sock = None
        self.rows = {}          # tree item -> (kind, port, ptype, bytes)
        self.counts = {}        # (kind, port, bytes) -> (item, n)
        self.pixels = blank()   # the drawing canvas (80 x 256)
        self.last = None        # last point while drawing
        self.history, self.hpos = [], 0
        self.preview_img = None
        self.canvas_img = None
        root.title("PictoChat host")
        root.geometry("1180x800")
        self.status = tk.StringVar(value="connecting to %s:%d ..." % self.addr)

        top = ttk.Frame(root)
        top.pack(fill="x", padx=6, pady=4)
        ttk.Label(top, textvariable=self.status).pack(side="left")
        ttk.Button(top, text="Clear lists", command=self.clear_log).pack(side="right")
        ttk.Button(top, text="Announce host", command=lambda: self.send({"cmd": "announce"})).pack(side="right", padx=6)

        paned = ttk.PanedWindow(root, orient="horizontal")
        paned.pack(fill="both", expand=True, padx=6)
        left = ttk.Frame(paned)
        paned.add(left, weight=3)
        self.tree = ttk.Treeview(left, columns=("time", "dir", "what", "n"), show="headings", selectmode="browse")
        for c, w in (("time", 70), ("dir", 40), ("what", 460), ("n", 40)):
            self.tree.heading(c, text=c)
            self.tree.column(c, width=w, anchor="w")
        sb = ttk.Scrollbar(left, command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.tree.tag_configure("rx", foreground="#1a5fb4")
        self.tree.tag_configure("tx", foreground="#26a269")
        self.tree.tag_configure("message", background="#fff3b0")
        self.tree.bind("<<TreeviewSelect>>", self.on_select)

        right = ttk.Frame(paned)
        paned.add(right, weight=2)
        self.preview = tk.Label(right, bd=1, relief="solid", bg="white")
        self.preview.pack(pady=(0, 4))
        self.detail = tk.Text(right, wrap="none", font=("monospace", 10), height=10)
        self.detail.pack(fill="both", expand=True)

        logf = ttk.Frame(root)
        logf.pack(fill="x", padx=6, pady=4)
        self.log = tk.Text(logf, height=7, font=("monospace", 9), state="disabled")
        self.log.pack(fill="x")

        bottom = ttk.Frame(root)
        bottom.pack(fill="x", padx=6, pady=4)
        left_b = ttk.Frame(bottom)
        left_b.pack(side="left", fill="x", expand=True)
        ttk.Label(left_b, text="Text to send as a chat message").pack(anchor="w")
        trow = ttk.Frame(left_b)
        trow.pack(fill="x")
        self.text_entry = ttk.Entry(trow)
        self.text_entry.pack(side="left", fill="x", expand=True)
        self.text_entry.bind("<Return>", lambda e: self.send_text())
        ttk.Button(trow, text="Send text", command=self.send_text).pack(side="left", padx=(6, 0))
        ttk.Label(left_b, text="> command (try: help). Enter or Send").pack(anchor="w", pady=(10, 0))
        crow = ttk.Frame(left_b)
        crow.pack(fill="x")
        self.entry = ttk.Entry(crow)
        self.entry.pack(side="left", fill="x", expand=True)
        ttk.Button(crow, text="Send", command=self.on_enter).pack(side="left", padx=(6, 0))
        self.entry.bind("<Return>", self.on_enter)
        self.entry.bind("<KP_Enter>", self.on_enter)
        self.entry.bind("<Up>", lambda e: self.hist(-1))
        self.entry.bind("<Down>", lambda e: self.hist(1))
        self.text_entry.focus_set()

        right_b = ttk.Frame(bottom)
        right_b.pack(side="right", padx=8)
        ttk.Label(right_b, text="Drawing (256 x 80; grey = not shown on the DS)").pack(anchor="w")
        self.canvas = tk.Canvas(right_b, width=PW * SCALE, height=PH * SCALE, bg="white",
                                highlightthickness=1, highlightbackground="#888")
        self.canvas.pack()
        self.canvas.bind("<ButtonPress-1>", self.pen_down)
        self.canvas.bind("<B1-Motion>", self.pen_move)
        self.canvas.bind("<ButtonRelease-1>", self.pen_up)
        brow = ttk.Frame(right_b)
        brow.pack(fill="x", pady=(4, 0))
        ttk.Button(brow, text="Send drawing", command=self.send_drawing).pack(side="left")
        ttk.Button(brow, text="Clear drawing", command=self.clear_canvas).pack(side="left", padx=6)
        ttk.Button(brow, text="Load image...", command=self.pick_image).pack(side="left")
        self.dither_var = tk.BooleanVar(value=True)
        self.invert_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(brow, text="dither", variable=self.dither_var, command=self.reload_image).pack(side="left", padx=(6, 0))
        ttk.Checkbutton(brow, text="invert", variable=self.invert_var, command=self.reload_image).pack(side="left")
        self.color_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(brow, text="colour", variable=self.color_var, command=self.reload_image).pack(side="left")
        self.image_path = None
        prow = ttk.Frame(right_b)
        prow.pack(fill="x", pady=(4, 0))
        ttk.Label(prow, text="pen:").pack(side="left")
        self.pen = 1
        self.swatches = {}
        for i in range(1, 16):
            r_, g_, b_ = PALETTE[i]
            sw = tk.Label(prow, width=2, bg="#%02x%02x%02x" % (r_, g_, b_), relief="raised", bd=2)
            sw.pack(side="left", padx=1)
            sw.bind("<Button-1>", lambda e, i=i: self.set_pen(i))
            self.swatches[i] = sw
        ttk.Label(prow, text="(also used for Send text)").pack(side="left", padx=4)
        self.set_pen(1)

        self.refresh_canvas()
        threading.Thread(target=self.net, daemon=True).start()
        root.after(100, self.pump)

    # ---- network ----
    def net(self):
        while True:
            try:
                s = socket.create_connection(self.addr, timeout=2)
            except OSError:
                self.q.put({"kind": "conn", "ok": False})
                time.sleep(1.0)
                continue
            s.settimeout(None)
            if os.environ.get("PICTOCHAT_TOKEN"):           # host started with --gui-token
                s.sendall((json.dumps({"cmd": "auth", "token": os.environ["PICTOCHAT_TOKEN"]}) + "\n").encode())
            self.sock = s
            self.q.put({"kind": "conn", "ok": True})
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
                            self.q.put(json.loads(line))
                        except ValueError:
                            pass
            except OSError:
                pass
            self.sock = None
            self.q.put({"kind": "conn", "ok": False})
            time.sleep(1.0)

    def send(self, obj):
        s = self.sock
        if not s:
            self.logline("# not connected to the host")
            return False
        try:
            s.sendall((json.dumps(obj) + "\n").encode())
            return True
        except OSError as e:
            self.logline("# send failed: %s" % e)
            return False

    def pump(self):
        try:
            while True:
                self.handle(self.q.get_nowait())
        except queue.Empty:
            pass
        self.root.after(100, self.pump)

    # ---- events ----
    def handle(self, ev):
        k = ev.get("kind")
        if k == "conn":
            self.status.set(("connected to %s:%d" if ev["ok"] else "waiting for host on %s:%d ...") % self.addr)
        elif k == "log":
            self.logline("%8.3f %s" % (ev.get("t", 0), ev["text"]))
        elif k in ("rx", "tx"):
            self.add_row(k, ev["port"], ev["ptype"], bytes.fromhex(ev["hex"]), ev.get("t", 0))
        elif k == "message":
            if ev.get("hex"):
                self.add_row("message-" + ev.get("dir", "rx"), 0xE, 2, bytes.fromhex(ev["hex"]), ev.get("t", 0), message=True)
        elif k == "state":
            self.status.set("connected | AIDs: %s | polls %d, replies %d, beacons %d" % (
                ", ".join("%s=%d" % kv for kv in ev.get("aids", {}).items()) or "none",
                ev.get("cmd", 0), ev.get("replies", 0), ev.get("beacons", 0)))

    def add_row(self, kind, port, ptype, data, t, message=False):
        d = kind.split("-")[1] if message else kind
        key = (kind, port, data)
        if key in self.counts:
            item, n = self.counts[key]
            self.counts[key] = (item, n + 1)
            self.tree.set(item, "n", n + 1)
            return
        item = self.tree.insert("", "end", values=("%.2f" % t, d, summarize(kind, port, ptype, data), 1),
                                tags=("message", d) if message else (d,))
        self.counts[key] = (item, 1)
        self.rows[item] = (kind, port, ptype, data)
        self.tree.see(item)

    def show_preview(self, rows):
        if rows is None:
            self.preview.configure(image="", width=1, height=1)
            self.preview_img = None
            return
        try:
            img = tk.PhotoImage(data=pixels_to_ppm(rows)).zoom(SCALE, SCALE)
        except tk.TclError:
            return
        self.preview_img = img
        self.preview.configure(image=img, width=PW * SCALE, height=PH * SCALE)

    def on_select(self, _e=None):
        sel = self.tree.selection()
        if not sel:
            return
        kind, port, ptype, data = self.rows[sel[0]]
        lines = ["%s  port 0x%x  type %d  %d bytes" % (kind, port, ptype, len(data)), ""]
        self.show_preview(decode_image(data) if kind.startswith("message") else None)
        mt = member_text(data) if kind.startswith("message") else None
        if mt:
            lines += ["member message " + mt, ""]
        lines += [hexdump(data[:512]) + ("\n... (%d more bytes)" % (len(data) - 512) if len(data) > 512 else ""), ""]
        s = utf16_guess(data)
        if s:
            lines.append("UTF-16 text guess: %r" % s)
        for off, m in swapped_macs(data):
            lines.append("byte-swapped MAC at offset %d: %s" % (off, m))
        if port == 0xE and ptype == 2 and len(data) >= 12 and not kind.startswith("message"):
            _t, _l, mid, _x, flen, last, off = struct.unpack_from("<HHBBBBI", data, 0)
            lines += ["", "fragment header: sender id %d, byte 5 = 0x%02x (stack garbage), %d data bytes, last=%d, offset %d" % (
                mid, data[5], flen, last, off), "data:", hexdump(data[12:12 + flen])]
        self.detail.delete("1.0", "end")
        self.detail.insert("1.0", "\n".join(lines))

    def logline(self, s):
        self.log.configure(state="normal")
        self.log.insert("end", s + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def clear_log(self):
        self.tree.delete(*self.tree.get_children())
        self.rows.clear()
        self.counts.clear()
        self.detail.delete("1.0", "end")
        self.show_preview(None)
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    # ---- the drawing ----
    def refresh_canvas(self):
        self.canvas.delete("all")
        try:
            self.canvas_img = tk.PhotoImage(data=pixels_to_ppm(self.pixels, shade=True)).zoom(SCALE, SCALE)
            self.canvas.create_image(0, 0, anchor="nw", image=self.canvas_img)
        except tk.TclError:
            pass

    def clear_canvas(self):
        self.pixels = blank()
        self.refresh_canvas()

    def pen_down(self, e):
        self.last = (min(PW - 1, e.x // SCALE), min(PH - 1, e.y // SCALE))
        draw_line(self.pixels, *self.last, *self.last, color=self.pen)
        self.canvas.create_rectangle(self.last[0] * SCALE, self.last[1] * SCALE, self.last[0] * SCALE + 2 * SCALE,
                                     self.last[1] * SCALE + 2 * SCALE, fill=self.pen_hex(), outline=self.pen_hex())

    def pen_move(self, e):
        if self.last is None:
            return
        x, y = max(0, min(PW - 1, e.x // SCALE)), max(0, min(PH - 1, e.y // SCALE))
        draw_line(self.pixels, self.last[0], self.last[1], x, y, color=self.pen)
        self.canvas.create_line(self.last[0] * SCALE + SCALE, self.last[1] * SCALE + SCALE, x * SCALE + SCALE,
                                y * SCALE + SCALE, width=2 * SCALE, capstyle="round", fill=self.pen_hex())
        self.last = (x, y)

    def pen_hex(self):
        return "#%02x%02x%02x" % PALETTE[self.pen]

    def pen_up(self, _e):
        self.last = None

    def set_pen(self, i):
        self.pen = i
        for k, sw in self.swatches.items():
            sw.configure(relief="sunken" if k == i else "raised")

    def pick_image(self):
        from tkinter import filedialog
        path = filedialog.askopenfilename(title="Image to send", filetypes=[
            ("Images", "*.png *.jpg *.jpeg *.gif *.bmp *.webp *.tif *.tiff"), ("All files", "*")])
        if path:
            self.load_image(path)

    def load_image(self, path):
        try:
            self.pixels = image_to_pixels(path, self.dither_var.get(), self.invert_var.get(), self.color_var.get())
        except ImportError:
            self.logline("# Pillow is not installed (pip install pillow): cannot load images")
            return
        except OSError as e:
            self.logline("# cannot read %s: %s" % (path, e))
            return
        self.image_path = path
        self.refresh_canvas()
        self.logline("# image %s dithered onto the canvas; press Send drawing to send it" % path)

    def reload_image(self):
        if self.image_path:
            self.load_image(self.image_path)

    def send_drawing(self):
        if not any(any(r) for r in self.pixels):
            self.logline("# the drawing is empty")
            return
        if self.send({"cmd": "chat", "pixels": clip(self.pixels)}):
            self.logline("# drawing sent to the host (grey areas are not shown by the DS and were left out)")

    def send_text(self):
        text = self.text_entry.get().strip()
        if not text:
            return
        px = text_to_pixels(text, self.pen)
        if px is None:
            self.logline("# Pillow is not installed: text cannot be rendered (pip install pillow); draw instead")
            return
        self.pixels = px
        self.refresh_canvas()
        if self.send({"cmd": "chat", "pixels": clip(px)}):
            self.logline("# text sent as a picture: %r" % text)
            self.text_entry.delete(0, "end")

    # ---- command line ----
    def hist(self, d):
        if not self.history:
            return
        self.hpos = max(0, min(len(self.history), self.hpos + d))
        self.entry.delete(0, "end")
        if self.hpos < len(self.history):
            self.entry.insert(0, self.history[self.hpos])

    def on_enter(self, _e=None):
        line = self.entry.get().strip()
        self.entry.delete(0, "end")
        if not line:
            return
        self.history.append(line)
        self.hpos = len(self.history)
        self.logline("> " + line)
        try:
            self.run_command(line)
        except Exception as e:  # noqa: BLE001
            self.logline("# error: %s" % e)

    def run_command(self, line):
        w = line.split()
        c = w[0].lower()
        if c == "help":
            self.logline(__doc__.split("Commands (command line at the bottom; also Enter):")[1].strip("\n"))
        elif c == "say":
            self.text_entry.delete(0, "end")
            self.text_entry.insert(0, line[3:].strip())
            self.send_text()
        elif c == "draw":
            self.send_drawing()
        elif c == "clear":
            self.clear_canvas()
        elif c == "pen":
            x, y = int(w[1]), int(w[2])
            draw_line(self.pixels, x, y, x, y, color=self.pen)
            self.refresh_canvas()
        elif c == "line":
            x1, y1, x2, y2 = (int(v) for v in w[1:5])
            draw_line(self.pixels, x1, y1, x2, y2, color=self.pen)
            self.refresh_canvas()
        elif c == "announce":
            self.send({"cmd": "announce", "name": w[1] if len(w) > 1 else ""})
        elif c == "raw":
            hx = w[2].replace(":", "")
            bytes.fromhex(hx)
            self.send({"cmd": "raw", "port": int(w[1], 0), "hex": hx, "repeats": int(w[3]) if len(w) > 3 else 6})
        elif c == "save":
            with open(w[1], "wb") as fh:
                fh.write(pixels_to_ppm(self.pixels))
            self.logline("# saved the canvas to %s" % w[1])
        elif c == "image":
            self.load_image(line[5:].strip())
        elif c == "clear-log":
            self.clear_log()
        else:
            self.logline("# unknown command, type help")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=7777)
    a = ap.parse_args()
    root = tk.Tk()
    App(root, a.host, a.port)
    root.mainloop()


if __name__ == "__main__":
    main()
