"""Picture helpers of the Home Assistant integration: decoding, signatures and matching. Needs Pillow only."""
import importlib.util
import os

import pictochat as host

spec = importlib.util.spec_from_file_location("picture", os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "custom_components", "pictochat", "picture.py"))
picture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(picture)

MAC = bytes.fromhex("0009bf123456")


def typed(text, dx=0, dy=0):
    rows = picture.text_to_pictures(text)[0]
    out = picture.blank()
    for y, r in enumerate(rows):
        for x, v in enumerate(r):
            if v and 0 <= y + dy < picture.PH and 0 <= x + dx < picture.PW:
                out[y + dy][x + dx] = v
    return out


def sig(text, **kw):
    return picture.Signature.from_pixels(typed(text, **kw))


def test_decode_matches_the_host_encoder():
    rows = typed("lights on")
    msg = host.chat_message(MAC, host.bitmap_to_image(rows))
    assert picture.is_chat(msg) and not picture.is_member(msg)
    assert picture.decode_image(msg) == rows
    assert picture.sender_mac(msg) == "00:09:bf:12:34:56"


def test_member_message():
    msg = host.member_info_message(1, MAC, "Sem")
    assert picture.is_member(msg) and picture.member_name(msg) == "Sem"
    assert picture.sender_mac(msg) == "00:09:bf:12:34:56"


def test_signature_roundtrip_and_blank():
    s = sig("lights on")
    again = picture.Signature.loads(s.dumps())
    assert (again.w, again.h, again.rows) == (s.w, s.h, s.rows)
    assert picture.Signature.from_pixels(picture.blank()) is None


def test_same_text_matches_anywhere_in_the_picture():
    assert picture.similarity(sig("lights on"), sig("lights on")) == 1.0
    assert picture.similarity(sig("lights on"), sig("lights on", dx=40, dy=16)) == 1.0
    assert picture.similarity(sig("lights on"), sig("lights on", dx=7, dy=3)) == 1.0


def test_other_texts_stay_below_the_default_threshold():
    a = sig("lights on")
    for other in ("lights off", "lights om", "Lights on", "light on", "tv on", "lights on!"):
        assert picture.similarity(a, sig(other)) < 0.97, other
    long = "switch the living room lights on"
    assert picture.similarity(sig(long), sig(long.replace("on", "an"))) < 0.97


def test_best_match_picks_the_right_phrase():
    phrases = [{"name": n, "samples": [sig(n).dumps()]} for n in ("lights on", "lights off", "tv")]
    phrases.append({"name": "broken", "samples": ["nonsense"]})
    p, score = picture.best_match(sig("lights off"), phrases)
    assert p["name"] == "lights off" and score == 1.0


def test_png():
    png = picture.to_png(typed("hi"))
    assert png[:8] == b"\x89PNG\r\n\x1a\n" and png[-8:-4] == b"IEND"


def test_text_is_split_and_stays_visible():
    pics = picture.text_to_pictures("word " * 120)
    assert len(pics) == 3
    for rows in pics:
        assert all(picture.visible(x, y) for y, r in enumerate(rows) for x, v in enumerate(r) if v)
