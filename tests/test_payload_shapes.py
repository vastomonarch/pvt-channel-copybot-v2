"""Offline TDLib payload-shape validator.

Field-type validation happens BEFORE the authorization gate in TDLib:
  * wrong-typed/known field  -> 400 "Failed to parse JSON object..."
  * unknown key              -> ignored
  * well-formed              -> 401 Unauthorized (auth gate)

So we can validate every request shape our code builds without credentials:
401 == shape accepted; 400 == shape bug.

Runs against the real bundled libtdjson (1.8.71).
"""
from __future__ import annotations

import ctypes
import ctypes.util
import glob
import importlib.util
import json
import os
import time
from pathlib import Path


def _load_tdjson():
    cands = []
    sys_lib = ctypes.util.find_library("tdjson")
    if sys_lib:
        cands.append(sys_lib)
    env = os.environ.get("TDLIB_LIBRARY_PATH", "")
    if env:
        cands.insert(0, env)
    try:
        spec = importlib.util.find_spec("tdjson")
        if spec and spec.submodule_search_locations:
            base = Path(list(spec.submodule_search_locations)[0])
            cands.extend(sorted(str(p) for p in
                                base.glob("../tdjson.libs/libtdjson*.so*")))
    except Exception:
        pass
    cands.extend(glob.glob(
        "/home/codespace/.python/current/lib/python3.14/site-packages/"
        "tdjson.libs/libtdjson*.so*"))
    for c in cands:
        if not c or (os.path.sep in c and not os.path.exists(c)):
            continue
        try:
            return ctypes.CDLL(c), c
        except OSError:
            continue
    raise RuntimeError("libtdjson not found")


LIB, LIB_PATH = _load_tdjson()
LIB.td_json_client_create.restype = ctypes.c_void_p
LIB.td_json_client_create.argtypes = []
LIB.td_json_client_send.restype = None
LIB.td_json_client_send.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
LIB.td_json_client_receive.restype = ctypes.c_char_p
LIB.td_json_client_receive.argtypes = [ctypes.c_void_p, ctypes.c_double]
LIB.td_json_client_destroy.restype = None
LIB.td_json_client_destroy.argtypes = [ctypes.c_void_p]
LIB.td_set_log_verbosity_level.argtypes = [ctypes.c_int]
LIB.td_set_log_verbosity_level(0)   # silence native stderr logging

PARAMS = {
    "@type": "setTdlibParameters", "use_test_dc": False,
    "database_directory": "/tmp/opencode/tdval/db",
    "files_directory": "/tmp/opencode/tdval/files",
    "database_encryption_key": "", "use_file_database": True,
    "use_chat_info_database": True, "use_message_database": True,
    "use_secret_chats": False, "api_id": 1, "api_hash": "0" * 32,
    "system_language_code": "en", "device_model": "v",
    "system_version": "linux", "application_version": "1.0",
}


def _client():
    h = LIB.td_json_client_create()
    LIB.td_json_client_send(h, json.dumps(PARAMS).encode())
    deadline = time.time() + 1.2
    while time.time() < deadline:
        LIB.td_json_client_receive(h, 0.3)
    return h


def _send(h, obj):
    LIB.td_json_client_send(h, json.dumps(obj).encode())


def _recv_until(h, pred, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        raw = LIB.td_json_client_receive(h, 1.0)
        if not raw:
            continue
        try:
            p = json.loads(raw.decode())
        except ValueError:
            continue
        if pred(p):
            return p
    return None


def classify(resp):
    """'OK' if the shape parsed (auth gate reached), else the parse error."""
    if resp is None:
        return "NO_RESPONSE"
    if resp.get("@type") == "error":
        code = resp.get("code")
        msg = str(resp.get("message", ""))
        if "Failed to parse JSON object" in msg or "Unknown class" in msg:
            return f"SHAPE_ERROR({code}): {msg}"
        return "OK"   # 401 Unauthorized / any non-parse error == shape valid
    return "OK"


def check(tag, payload):
    h = _client()
    try:
        _send(h, payload)
        resp = _recv_until(
            h, lambda x: x.get("@type") in ("error", "message", "messages",
                                            "formattedText", "chat", "user",
                                            "ok", "messages"))
        return tag, classify(resp)
    finally:
        LIB.td_json_client_destroy(h)


# ---------------------------------------------------------------------------
# builders mirroring the real code
# ---------------------------------------------------------------------------
def fmt(text="hello", entities=None):
    return {"@type": "formattedText", "text": text, "entities": entities or []}


def input_text(text="hello", entities=None, lpo=None):
    return {"@type": "inputMessageText", "text": fmt(text, entities),
            "link_preview_options": lpo, "clear_draft": False}


def inline_kb(rows):
    return {"@type": "replyMarkupInlineKeyboard", "rows": rows}


def btn(label, data, *, style=True, icon=True):
    b = {"@type": "inlineKeyboardButton", "text": label,
         "type": {"@type": "inlineKeyboardButtonTypeCallback",
                  "data": data}}
    if style:
        b["style"] = {"@type": "buttonStyleDefault"}
    if icon:
        b["icon_custom_emoji_id"] = 0
    return b


def send_msg(content=None, markup=None, *, topic=True):
    return {"@type": "sendMessage", "chat_id": 1,
            "topic_id": None if topic else 0,
            "reply_to": None,
            "options": {"@type": "messageSendOptions",
                        "disable_notification": False},
            "reply_markup": markup,
            "input_message_content": content or input_text()}


def cases():
    yield "sendMessage/text", send_msg()
    yield "sendMessage/text+keyboard", send_msg(
        markup=inline_kb([[btn("R", "cmVmcmVzaA=="), btn("C", "Y2FuY2Vs")]]))
    yield "sendMessage/text+keyboard(no style/icon)", send_msg(
        markup=inline_kb([[btn("R", "eA==", style=False, icon=False)]]))
    yield "sendMessage/text+url-button", send_msg(markup=inline_kb([[
        {"@type": "inlineKeyboardButton", "text": "open",
         "type": {"@type": "inlineKeyboardButtonTypeUrl",
                  "url": "https://example.com"}}]]))
    yield "sendMessage/no markup", send_msg(markup=None)

    yield "editMessageText", {
        "@type": "editMessageText", "chat_id": 1, "message_id": 1,
        "reply_markup": inline_kb([[btn("R", "eA==")]]),
        "input_message_content": input_text()}
    yield "forwardMessages/copy", {
        "@type": "forwardMessages", "chat_id": 1, "topic_id": None,
        "from_chat_id": 2, "message_ids": [1, 2, 3], "options": None,
        "send_copy": True, "remove_caption": False}
    yield "deleteMessages", {
        "@type": "deleteMessages", "chat_id": 1,
        "message_ids": [1, 2], "revoke": True}
    yield "answerCallbackQuery", {
        "@type": "answerCallbackQuery", "callback_query_id": 123,
        "text": "hi", "show_alert": False, "url": "", "cache_time": 0}
    yield "getMe", {"@type": "getMe"}
    yield "getChat", {"@type": "getChat", "chat_id": 1}
    yield "getMessages", {"@type": "getMessages", "chat_id": 1,
                          "message_ids": [1, 2]}
    yield "getMessage", {"@type": "getMessage", "chat_id": 1,
                         "message_id": 1}
    yield "getChatHistory", {"@type": "getChatHistory", "chat_id": 1,
                             "from_message_id": 0, "offset": 0, "limit": 40,
                             "only_local": False}
    yield "searchPublicChat", {"@type": "searchPublicChat",
                               "username": "telegram"}
    yield "parseTextEntities/HTML", {
        "@type": "parseTextEntities", "text": "<b>hi</b> <pre>x</pre>",
        "parse_mode": {"@type": "textParseModeHTML"}}
    yield "downloadFile", {
        "@type": "downloadFile", "file_id": 1, "priority": 1,
        "offset": 0, "limit": 0, "synchronous": False}
    yield "cancelDownloadFile", {
        "@type": "cancelDownloadFile", "file_id": 1, "delete_file": False}
    yield "close", {"@type": "close"}

    # ── InputMessageContent shapes from build_input_content ──────────────
    cap = {"@type": "formattedText", "text": "cap", "entities": []}
    yield "inputMessageText", send_msg(input_text())
    yield "inputMessageContact", send_msg({"@type": "inputMessageContact",
        "contact": {"@type": "contact", "phone_number": "1", "first_name": "a",
                    "last_name": "", "user_id": 0, "vcard": ""}})
    yield "inputMessageLocation", send_msg({"@type": "inputMessageLocation",
        "location": {"@type": "location", "latitude": 1.0, "longitude": 2.0,
                     "horizontal_accuracy": 0.0, "vertical_accuracy": 0.0,
                     "altitude": 0.0, "heading": 0, "period": 0,
                     "live_period": 0, "accuracy": 0}})
    yield "inputMessageVenue", send_msg({"@type": "inputMessageVenue",
        "venue": {"@type": "venue", "location":
                  {"@type": "location", "latitude": 1.0, "longitude": 2.0,
                   "horizontal_accuracy": 0.0, "vertical_accuracy": 0.0,
                   "altitude": 0.0, "heading": 0, "period": 0,
                   "live_period": 0, "accuracy": 0},
                  "title": "t", "address": "a", "provider": "",
                  "external_url": "", "id": "", "type": ""}})
    yield "inputMessagePhoto", send_msg({"@type": "inputMessagePhoto",
        "photo": {"@type": "inputPhoto", "photo":
                  {"@type": "inputFileLocal", "path": "/tmp/x.jpg"},
                  "thumbnail": None, "video": None,
                  "added_sticker_file_ids": [], "width": 1, "height": 1},
        "caption": cap, "show_caption_above_media": False,
        "self_destruct_type": None, "has_spoiler": False})
    yield "inputMessageVideo", send_msg({"@type": "inputMessageVideo",
        "video": {"@type": "inputVideo", "video":
                  {"@type": "inputFileLocal", "path": "/tmp/x.mp4"},
                  "thumbnail": None, "cover": None, "start_timestamp": 0,
                  "added_sticker_file_ids": [], "duration": 1, "width": 1,
                  "height": 1, "supports_streaming": True},
        "caption": cap, "show_caption_above_media": False,
        "self_destruct_type": None, "has_spoiler": False})
    yield "inputMessageAnimation", send_msg({"@type": "inputMessageAnimation",
        "animation": {"@type": "inputAnimation", "animation":
                      {"@type": "inputFileLocal", "path": "/tmp/x.gif"},
                      "thumbnail": None, "added_sticker_file_ids": [],
                      "duration": 1, "width": 1, "height": 1},
        "caption": cap, "show_caption_above_media": False,
        "has_spoiler": False})
    yield "inputMessageAudio", send_msg({"@type": "inputMessageAudio",
        "audio": {"@type": "inputAudio", "audio":
                  {"@type": "inputFileLocal", "path": "/tmp/x.mp3"},
                  "album_cover_thumbnail": None, "duration": 1,
                  "title": "t", "performer": "p"},
        "caption": cap})
    yield "inputMessageVoiceNote", send_msg({"@type": "inputMessageVoiceNote",
        "voice_note": {"@type": "inputVoiceNote", "voice_note":
                       {"@type": "inputFileLocal", "path": "/tmp/x.ogg"},
                       "duration": 1, "waveform": ""},
        "caption": cap, "self_destruct_type": None})
    yield "inputMessageVideoNote", send_msg({"@type": "inputMessageVideoNote",
        "video_note": {"@type": "inputVideoNote", "video_note":
                       {"@type": "inputFileLocal", "path": "/tmp/x.mp4"},
                       "thumbnail": None, "duration": 1, "length": 1},
        "self_destruct_type": None})
    yield "inputMessageDocument", send_msg({"@type": "inputMessageDocument",
        "document": {"@type": "inputDocument", "document":
                     {"@type": "inputFileLocal", "path": "/tmp/x.bin"},
                     "thumbnail": None,
                     "disable_content_type_detection": False},
        "caption": cap})

    # thumbnail wrapper
    yield "inputMessagePhoto+thumb", send_msg({"@type": "inputMessagePhoto",
        "photo": {"@type": "inputPhoto", "photo":
                  {"@type": "inputFileLocal", "path": "/tmp/x.jpg"},
                  "thumbnail": {"@type": "inputThumbnail", "thumbnail":
                                {"@type": "inputFileLocal",
                                 "path": "/tmp/t.jpg"},
                                "width": 1, "height": 1},
                  "video": None, "added_sticker_file_ids": [],
                  "width": 1, "height": 1},
        "caption": cap, "show_caption_above_media": False,
        "self_destruct_type": None, "has_spoiler": False})

    # album
    yield "sendMessageAlbum", {
        "@type": "sendMessageAlbum", "chat_id": 1, "topic_id": None,
        "reply_to": None, "options": None, "reply_markup": None,
        "input_message_contents": [
            {"@type": "inputMessagePhoto",
             "photo": {"@type": "inputPhoto", "photo":
                       {"@type": "inputFileLocal", "path": "/tmp/a.jpg"},
                       "thumbnail": None, "video": None,
                       "added_sticker_file_ids": [], "width": 1, "height": 1},
             "caption": cap, "show_caption_above_media": False,
             "self_destruct_type": None, "has_spoiler": False},
            {"@type": "inputMessagePhoto",
             "photo": {"@type": "inputPhoto", "photo":
                       {"@type": "inputFileLocal", "path": "/tmp/b.jpg"},
                       "thumbnail": None, "video": None,
                       "added_sticker_file_ids": [], "width": 1, "height": 1},
             "caption": cap, "show_caption_above_media": False,
             "self_destruct_type": None, "has_spoiler": False}]}

    # poll-as-text (built by _poll_as_text)
    yield "inputMessageText/poll-card", send_msg(input_text(
        "📊 Poll\nQuestion: Q\n  1. a\n  2. b",
        [{"@type": "textEntity", "offset": 0, "length": 5,
          "type": {"@type": "textEntityTypeBold"}}]))


def main():
    print(f"libtdjson: {LIB_PATH}")
    ok = bad = 0
    for tag, payload in cases():
        _, result = check(tag, payload)
        status = "PASS" if result == "OK" else "FAIL"
        if result == "OK":
            ok += 1
        else:
            bad += 1
        print(f"  [{status}] {tag:38s} {result}")
    print(f"\n{ok} passed, {bad} failed")
    return bad


if __name__ == "__main__":
    raise SystemExit(1 if main() else 0)
