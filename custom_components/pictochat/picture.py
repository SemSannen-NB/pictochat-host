"""PictoChat pictures: decoding, matching against learned phrases, PNG export and rendering text to send.

A DS never sends text. Every chat message is a 256 x 80 picture of palette indices (PROTOCOL.md section 7), and
typed letters are stamped into it as pixels. The same typed text gives the same pixels every time, so a message is
recognised by comparing its ink with samples learned earlier. Nothing here needs Home Assistant.
"""
from __future__ import annotations

import base64
import struct
import zlib

PW, PH = 256, 80
# What a DS shows of a picture: columns 23-252; the sender's name tab covers columns 0-80 of rows 0-15.
VIS_X0, VIS_X1, TAB_X1, TAB_Y1 = 23, 252, 80, 15
TEXT_X0, TEXT_X1, TEXT_LINES = 26, 250, 4      # text we send goes on lines 2-5, inside the visible columns
PALETTE = [(251, 251, 251), (0, 0, 0), (211, 203, 195), (235, 0, 235), (251, 0, 138), (251, 0, 40), (251, 73, 0),
           (251, 162, 0), (227, 243, 0), (130, 251, 0), (16, 251, 32), (0, 251, 186), (0, 195, 251), (0, 121, 251),
           (0, 48, 251), (40, 0, 251)]
MAX_SHIFT = 2                                  # a sample may sit this many pixels off and still match


def visible(x: int, y: int) -> bool:
    return VIS_X0 <= x <= VIS_X1 and not (x <= TAB_X1 and y <= TAB_Y1)


def blank() -> list[list[int]]:
    return [[0] * PW for _ in range(PH)]


def clip(rows: list[list[int]]) -> list[list[int]]:
    return [[v if visible(x, y) else 0 for x, v in enumerate(r)] for y, r in enumerate(rows)]


# ---------------------------------------------------------------- messages from the host
def is_member(msg: bytes) -> bool:
    return len(msg) >= 28 and msg[0] == 3 and msg[1] in (0, 1)


def is_chat(msg: bytes) -> bool:
    return len(msg) >= 36 + 2048 and msg[0] == 3 and msg[1] == 2


def sender_mac(msg: bytes) -> str:
    """MAC of the DS that sent a member or chat message (stored with each byte pair swapped)."""
    return ":".join("%02x" % msg[2 + (i ^ 1)] for i in range(6))


def member_name(msg: bytes) -> str:
    return msg[8:28].decode("utf-16-le", "replace").split("\x00")[0]


def decode_image(msg: bytes) -> list[list[int]] | None:
    """Pixels (80 rows of 256 palette indices) of a `03 02` chat message, else None."""
    if not is_chat(msg):
        return None
    data = msg[36:36 + 10240]                  # 1-5 lines of 2048 bytes: 8x8 tiles, 4 bits per pixel
    rows = blank()
    for t in range(len(data) // 32):
        tx, ty = t % 32, t // 32
        for y in range(8):
            for x in range(8):
                b = data[t * 32 + y * 4 + x // 2]
                rows[ty * 8 + y][tx * 8 + x] = (b >> 4) if x & 1 else (b & 15)
    return rows


# ---------------------------------------------------------------- signatures and matching
class Signature:
    """The ink of a picture, cropped to its bounding box. Colour and position in the picture are ignored."""

    __slots__ = ("w", "h", "rows", "ink")

    def __init__(self, w: int, h: int, rows: list[int]):
        self.w, self.h, self.rows = w, h, rows          # each row is an int; bit x is the pixel in column x
        self.ink = sum(r.bit_count() for r in rows)

    @classmethod
    def from_pixels(cls, pixels: list[list[int]]) -> "Signature | None":
        """None for a picture without visible ink."""
        bits = [sum(1 << x for x, v in enumerate(r) if v and visible(x, y)) for y, r in enumerate(pixels)]
        ys = [y for y, b in enumerate(bits) if b]
        if not ys:
            return None
        allbits = 0
        for b in bits:
            allbits |= b
        x0 = (allbits & -allbits).bit_length() - 1
        rows = [b >> x0 for b in bits[ys[0]:ys[-1] + 1]]
        return cls(allbits.bit_length() - x0, len(rows), rows)

    def dumps(self) -> str:
        nb = (self.w + 7) // 8
        raw = b"".join(r.to_bytes(nb, "little") for r in self.rows)
        return "%dx%d:%s" % (self.w, self.h, base64.b64encode(zlib.compress(raw, 9)).decode())

    @classmethod
    def loads(cls, s: str) -> "Signature":
        size, data = s.split(":", 1)
        w, h = (int(v) for v in size.split("x"))
        nb = (w + 7) // 8
        raw = zlib.decompress(base64.b64decode(data))
        if w < 1 or h < 1 or len(raw) != nb * h:
            raise ValueError("bad signature")
        return cls(w, h, [int.from_bytes(raw[i * nb:(i + 1) * nb], "little") for i in range(h)])


def similarity(a: Signature, b: Signature) -> float:
    """0.0-1.0: the share of ink the two pictures have in common (intersection over union), at the best alignment
    within MAX_SHIFT pixels. Two messages typed with the same letters score 1.0."""
    if abs(a.w - b.w) > 2 * MAX_SHIFT or abs(a.h - b.h) > 2 * MAX_SHIFT:
        return 0.0
    total = a.ink + b.ink
    best = 0.0
    # try the exact alignment first: typed text matches there and needs no search
    shifts = [(0, 0)] + [(dx, dy) for dy in range(-MAX_SHIFT, MAX_SHIFT + 1)
                         for dx in range(-MAX_SHIFT, MAX_SHIFT + 1) if (dx, dy) != (0, 0)]
    for dx, dy in shifts:
        common = 0
        for y, ra in enumerate(a.rows):
            yb = y + dy
            if 0 <= yb < b.h:
                rb = b.rows[yb]
                common += (ra & (rb << dx if dx >= 0 else rb >> -dx)).bit_count()
        score = common / (total - common)
        if score > best:
            best = score
            if best == 1.0:
                break
    return best


def best_match(sig: Signature, phrases: list[dict]) -> tuple[dict | None, float]:
    """The phrase whose samples fit `sig` best, and its score. A phrase is {"samples": [dumped signature, ...]}."""
    best, score = None, 0.0
    for p in phrases:
        for s in p.get("samples", []):
            try:
                v = similarity(sig, Signature.loads(s))
            except (ValueError, zlib.error):
                continue
            if v > score:
                best, score = p, v
    return best, score


# ---------------------------------------------------------------- export
def to_png(pixels: list[list[int]], scale: int = 3) -> bytes:
    """The picture as a PNG with PictoChat's palette, enlarged `scale` times."""
    raw = bytearray()
    for r in pixels:
        line = b"\x00" + bytes(v & 15 for v in r for _ in range(scale))
        raw += line * scale

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", PW * scale, PH * scale, 8, 3, 0, 0, 0))
            + chunk(b"PLTE", bytes(c for rgb in PALETTE for c in rgb))
            + chunk(b"IDAT", zlib.compress(bytes(raw), 6)) + chunk(b"IEND", b""))


# ---------------------------------------------------------------- pictures to send (these need Pillow)
def _font():
    from PIL import ImageFont
    for name in ("DejaVuSans.ttf", "FreeSans.ttf", "LiberationSans-Regular.ttf"):
        try:
            return ImageFont.truetype(name, 13)
        except OSError:
            continue
    try:
        return ImageFont.load_default(13)
    except TypeError:                           # Pillow before 10.1: one fixed size
        return ImageFont.load_default()


def text_to_pictures(text: str, color: int = 1, max_pictures: int = 3) -> list[list[list[int]]]:
    """Word-wraps text and renders it into one or more pictures of TEXT_LINES lines each."""
    from PIL import Image, ImageDraw
    font = _font()
    d = ImageDraw.Draw(Image.new("L", (1, 1)))
    lines: list[str] = []
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
    out = []
    for i in range(0, len(lines), TEXT_LINES):
        img = Image.new("L", (PW, PH), 0)
        d = ImageDraw.Draw(img)
        for n, ln in enumerate(lines[i:i + TEXT_LINES]):
            d.text((TEXT_X0, 17 + 16 * n), ln, fill=255, font=font)
        px = img.load()
        rows = clip([[color if px[x, y] > 110 else 0 for x in range(PW)] for y in range(PH)])
        if any(any(r) for r in rows):
            out.append(rows)
    return out[:max_pictures]


def image_to_picture(path: str, dither: bool = True, color: bool = False) -> list[list[int]]:
    """Fits an image file into the area a DS shows below the name tab (230 x 64 px) and converts it to black ink,
    or to PictoChat's 16 colours with color=True."""
    from PIL import Image, ImageOps
    img = ImageOps.exif_transpose(Image.open(path)).convert("RGBA")
    img = Image.alpha_composite(Image.new("RGBA", img.size, (255, 255, 255, 255)), img).convert("RGB")
    bw, bh = VIS_X1 - VIS_X0 + 1, PH - (TAB_Y1 + 1)
    mode = Image.FLOYDSTEINBERG if dither else Image.NONE
    if color:
        img.thumbnail((bw, bh), Image.LANCZOS)
        pal = Image.new("P", (1, 1))
        flat = [c for rgb in PALETTE for c in rgb]
        pal.putpalette(flat + flat[-3:] * (256 - 16))
        out = img.quantize(palette=pal, dither=mode)
        value = lambda v: min(v, 15)            # noqa: E731
    else:
        img = ImageOps.autocontrast(img.convert("L"), cutoff=1)
        img.thumbnail((bw, bh), Image.LANCZOS)
        out = img.convert("1", dither=mode)
        value = lambda v: 0 if v else 1         # noqa: E731  (black = ink)
    ox = VIS_X0 + (bw - out.width) // 2
    oy = TAB_Y1 + 1 + (bh - out.height) // 2
    rows = blank()
    px = out.load()
    for y in range(out.height):
        for x in range(out.width):
            rows[oy + y][ox + x] = value(px[x, y])
    return rows
