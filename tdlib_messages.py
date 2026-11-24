"""Chat & message operations on top of :class:`TDClient`.

Everything the copy pipeline needs:

* resolving ``t.me`` links / usernames / raw ids to TDLib chat ids,
* batch message fetching with per-id fallback,
* message classification by TDLib ``MessageContent`` type,
* sending / editing / copying / deleting messages with **awaited send
  completion** (so retries can never duplicate a finished upload),
* inline-button callback answers.

Send-completion race
--------------------
``sendMessage`` returns a *pending* message; the final result arrives later
as ``updateMessageSendSucceeded``.  Subscribing after the response would
race a fast completion.  A per-client :class:`SendTracker` subscribes once,
caches completions, and lets :func:`wait_sent` await a completion that may
already have happened — closing the race without polling.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any, Iterable

from errors import TdError
from tdlib_client import TDClient

log = logging.getLogger("copybot.tdlib.messages")

__all__ = [
    "parse_message_link",
    "resolve_chat",
    "chat_title",
    "get_me",
    "sender_user_id",
    "get_messages",
    "fetch_window",
    "classify",
    "content_of",
    "caption_of",
    "media_file",
    "media_name",
    "media_size",
    "send_text",
    "edit_message",
    "send_input",
    "send_album",
    "copy_messages",
    "delete_messages",
    "answer_callback_query",
    "wait_sent",
    "SendTracker",
    "get_send_tracker",
]


# ---------------------------------------------------------------------------
# Link parsing / chat resolution
# ---------------------------------------------------------------------------
PRIVATE_LINK_RE = re.compile(
    r"(?:https?://)?t\.me/c/(?P<channel_id>\d+)/(?P<msg_id>\d+)")
PUBLIC_LINK_RE = re.compile(
    r"(?:https?://)?t\.me/(?P<username>[a-zA-Z0-9_]{5,32})/(?P<msg_id>\d+)")


def parse_message_link(link: str) -> tuple[int | str, int] | None:
    """Return ``(chat_id_or_username, message_id)`` for a t.me link."""
    m = PRIVATE_LINK_RE.search(link)
    if m:
        return int(f"-100{m.group('channel_id')}"), int(m.group("msg_id"))
    m = PUBLIC_LINK_RE.search(link)
    if m:
        return m.group("username"), int(m.group("msg_id"))
    return None


async def resolve_chat(client: TDClient, identifier: int | str) -> int:
    """Resolve a raw id, ``-100…`` id, or @username to a TDLib chat id."""
    if isinstance(identifier, int):
        chat_id = identifier
    else:
        ident = str(identifier).strip()
        if re.fullmatch(r"-?\d+", ident):
            chat_id = int(ident)
        else:
            username = ident.lstrip("@")
            result = await client.request(
                {"@type": "searchPublicChat", "username": username})
            return int(result["id"])
    # Validate that we know/access the chat: getChat fails if unknown.
    await client.request({"@type": "getChat", "chat_id": chat_id})
    return chat_id


async def chat_title(client: TDClient, chat_id: int) -> str:
    try:
        chat = await client.request({"@type": "getChat", "chat_id": chat_id})
    except TdError:
        return str(chat_id)
    return str(chat.get("title") or chat_id)


async def get_me(client: TDClient) -> dict:
    return await client.request({"@type": "getMe"})


def sender_user_id(message: dict) -> int | None:
    sender = message.get("sender_id") or {}
    if sender.get("@type") == "messageSenderUser":
        return int(sender.get("user_id", 0)) or None
    return None


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------
async def get_messages(client: TDClient, chat_id: int,
                       message_ids: Iterable[int]) -> dict[int, dict | None]:
    """Batch-fetch messages.

    ``getMessages`` returns nulls for ids it cannot resolve; those are
    retried one by one with ``getMessage`` (a server-backed method that
    404s for deleted messages).  Returns ``{id: message_or_None}`` for
    every requested id.
    """
    ids = [int(i) for i in message_ids]
    if not ids:
        return {}
    out: dict[int, dict | None] = {i: None for i in ids}
    try:
        res = await client.request({
            "@type": "getMessages",
            "chat_id": chat_id,
            "message_ids": ids,
        })
        for msg in res.get("messages") or []:
            if isinstance(msg, dict) and msg.get("@type") == "message":
                out[int(msg["id"])] = msg
    except TdError as exc:
        log.debug("getMessages failed (%s); falling back per id", exc)

    missing = [i for i, m in out.items() if m is None]
    for mid in missing:
        try:
            msg = await client.request(
                {"@type": "getMessage", "chat_id": chat_id,
                 "message_id": mid})
            if isinstance(msg, dict) and msg.get("@type") == "message":
                out[mid] = msg
        except TdError as exc:
            from errors import classify
            if classify(exc).skip_message or exc.code == 404:
                continue
            raise
    return out


async def fetch_window(client: TDClient, chat_id: int, center_id: int,
                       start_id: int, end_id: int,
                       window: int) -> dict[int, dict | None]:
    """Fetch ``[center-window, center+window]`` clamped to the job range."""
    lo = max(start_id, center_id - window)
    hi = min(end_id, center_id + window)
    return await get_messages(client, chat_id, range(lo, hi + 1))


# ---------------------------------------------------------------------------
# Classification & content helpers
# ---------------------------------------------------------------------------
MEDIA_TYPES = {
    "messagePhoto", "messageDocument", "messageVideo", "messageAnimation",
    "messageAudio", "messageVoiceNote", "messageVideoNote", "messageSticker",
}
_TEXT_TYPES = {"messageText"}
_POLL_TYPES = {"messagePoll"}
_CONTACT_TYPES = {"messageContact"}
_LOCATION_TYPES = {"messageLocation", "messageVenue"}


def content_of(message: dict) -> dict:
    return message.get("content") or {}


def classify(message: dict | None) -> str:
    """Map a message to ``text|media|poll|contact|location|service``.

    Mirrors the legacy Telethon classifier, including its treatment of
    link previews as plain text and service/unsupported messages as
    skippable.
    """
    if not message or message.get("@type") != "message":
        return "service"
    content = content_of(message)
    ctype = content.get("@type", "")
    if ctype in _TEXT_TYPES:
        return "text"
    if ctype in MEDIA_TYPES:
        return "media"
    if ctype in _POLL_TYPES:
        return "poll"
    if ctype in _CONTACT_TYPES:
        return "contact"
    if ctype in _LOCATION_TYPES:
        return "location"
    return "service"   # joins/leaves, unsupported, empty, …


def caption_of(content: dict) -> tuple[str, list[dict]]:
    """(caption text, entities) for a media content, both may be empty."""
    caption = content.get("caption") or {}
    return str(caption.get("text") or ""), list(caption.get("entities") or [])


def media_file(content: dict) -> dict | None:
    """The TDLib ``file`` object carrying the payload of *content*."""
    t = content.get("@type")
    if t == "messagePhoto":
        photo = content.get("photo") or {}
        sizes = photo.get("sizes") or []
        best: dict | None = None
        best_size = -1
        for sz in sizes:
            f = sz.get("photo") or {}
            size = int(f.get("size") or f.get("expected_size") or 0)
            if size > best_size:
                best_size, best = size, f
        return best
    if t == "messageDocument":
        return (content.get("document") or {}).get("document")
    if t == "messageVideo":
        return (content.get("video") or {}).get("video")
    if t == "messageAnimation":
        return (content.get("animation") or {}).get("animation")
    if t == "messageAudio":
        return (content.get("audio") or {}).get("audio")
    if t == "messageVoiceNote":
        return (content.get("voice_note") or {}).get("voice")
    if t == "messageVideoNote":
        return (content.get("video_note") or {}).get("video")
    if t == "messageSticker":
        return (content.get("sticker") or {}).get("sticker")
    return None


_NAME_KEY_BY_TYPE = {
    "messageDocument": "document",
    "messageVideo": "video",
    "messageAnimation": "animation",
    "messageAudio": "audio",
}


def media_name(content: dict, default: str = "") -> str:
    key = _NAME_KEY_BY_TYPE.get(content.get("@type", ""))
    if key:
        name = (content.get(key) or {}).get("file_name")
        if name:
            return str(name)
    return default


def media_size(content: dict) -> int:
    f = media_file(content) or {}
    try:
        return int(f.get("size") or f.get("expected_size") or 0)
    except (TypeError, ValueError):
        return 0


# ---------------------------------------------------------------------------
# Send-completion tracking
# ---------------------------------------------------------------------------
class SendTracker:
    """Per-client cache/waiter for ``updateMessageSendSucceeded|Failed``."""

    _MAX = 2048

    def __init__(self, client: TDClient):
        self._client = client
        self._done: dict[tuple[int, int], dict] = {}
        self._waiters: dict[tuple[int, int], list[Any]] = {}
        self._key = client.router.subscribe(
            self._on_update,
            ("updateMessageSendSucceeded", "updateMessageSendFailed"))

    def _on_update(self, payload: dict) -> None:
        key = (int(payload.get("chat_id", 0)),
               int(payload.get("old_message_id", 0)))
        self._done[key] = payload
        if len(self._done) > self._MAX:
            for stale in list(self._done)[: self._MAX // 4]:
                del self._done[stale]
        for fut in self._waiters.pop(key, []):
            if not fut.done():
                fut.set_result(payload)

    async def wait(self, chat_id: int, old_id: int,
                   timeout: float) -> dict:
        key = (int(chat_id), int(old_id))
        cached = self._done.pop(key, None)
        if cached is not None:
            return self._check(cached)
        fut = self._client.loop.create_future()
        self._waiters.setdefault(key, []).append(fut)
        try:
            payload = await _await_future(fut, timeout)
        finally:
            lst = self._waiters.get(key, [])
            if fut in lst:
                lst.remove(fut)
            if not lst:
                self._waiters.pop(key, None)
        return self._check(payload)

    @staticmethod
    def _check(payload: dict) -> dict:
        if payload.get("@type") == "updateMessageSendFailed":
            err = payload.get("error") or {}
            raise TdError(int(err.get("code", 500)),
                          str(err.get("message", "send failed")))
        return payload.get("message") or {}

    def close(self) -> None:
        try:
            self._client.router.unsubscribe(self._key)
        except Exception:  # pragma: no cover
            pass
        for lst in self._waiters.values():
            for fut in lst:
                if not fut.done():
                    fut.cancel()
        self._waiters.clear()
        self._done.clear()


async def _await_future(fut, timeout: float) -> dict:
    try:
        return await asyncio.wait_for(fut, timeout)
    except asyncio.TimeoutError:
        raise TdError(500, f"send completion timed out after {timeout:.0f}s")


def get_send_tracker(client: TDClient) -> SendTracker:
    tracker = getattr(client, "_send_tracker", None)
    if tracker is None:
        tracker = SendTracker(client)
        client._send_tracker = tracker
    return tracker


async def wait_sent(client: TDClient, chat_id: int, message: dict,
                    timeout: float) -> dict:
    """Await completion of a just-sent message; raise TdError on failure."""
    sending = message.get("sending_state")
    if sending and sending.get("@type") == "messageSendingStateFailed":
        err = sending.get("error") or {}
        raise TdError(int(err.get("code", 500)),
                      str(err.get("message", "send failed")))
    if not sending or sending.get("@type") != "messageSendingStatePending":
        return message   # already complete (fast path)
    return await get_send_tracker(client).wait(
        chat_id, int(message["id"]), timeout)


# ---------------------------------------------------------------------------
# Sending / editing / copying / deleting
# ---------------------------------------------------------------------------
async def send_text(client: TDClient, chat_id: int, text: str,
                    *, entities: list[dict] | None = None,
                    reply_markup: dict | None = None,
                    disable_notification: bool = False,
                    timeout: float | None = None) -> dict:
    content: dict[str, Any] = {
        "@type": "inputMessageText",
        "text": {"@type": "formattedText", "text": text,
                 "entities": entities or []},
        "link_preview_options": None,
        "clear_draft": False,
    }
    return await send_input(client, chat_id, content,
                            reply_markup=reply_markup,
                            disable_notification=disable_notification,
                            timeout=timeout)


async def send_input(client: TDClient, chat_id: int,
                     input_content: dict, *,
                     reply_markup: dict | None = None,
                     disable_notification: bool = False,
                     timeout: float | None = None) -> dict:
    """Send one input message content and await server confirmation."""
    sent = await client.request_long({
        "@type": "sendMessage",
        "chat_id": chat_id,
        "topic_id": None,
        "reply_to": None,
        "options": {
            "@type": "messageSendOptions",
            "disable_notification": disable_notification,
        } if disable_notification else None,
        "reply_markup": reply_markup,
        "input_message_content": input_content,
    }, timeout=timeout)
    return await wait_sent(client, chat_id, sent,
                           timeout if timeout is not None else 1800.0)


async def send_album(client: TDClient, chat_id: int,
                     contents: list[dict], *, timeout: float | None = None,
                     reply_markup: dict | None = None) -> list[dict]:
    """Send grouped media; awaits completion of every part (order kept)."""
    sent = await client.request_long({
        "@type": "sendMessageAlbum",
        "chat_id": chat_id,
        "topic_id": None,
        "reply_to": None,
        "options": None,
        "reply_markup": reply_markup,
        "input_message_contents": contents,
    }, timeout=timeout)
    messages = sent.get("messages") or []
    out: list[dict] = []
    effective_timeout = timeout if timeout is not None else 1800.0
    for msg in messages:
        out.append(await wait_sent(client, chat_id, msg, effective_timeout))
    return out


async def copy_messages(client: TDClient, from_chat_id: int,
                        chat_id: int, message_ids: list[int], *,
                        timeout: float | None = None) -> list[dict]:
    """Server-side copy (no "Forwarded from" tag, no re-upload)."""
    sent = await client.request_long({
        "@type": "forwardMessages",
        "chat_id": chat_id,
        "topic_id": None,
        "from_chat_id": from_chat_id,
        "message_ids": message_ids,
        "options": None,
        "send_copy": True,
        "remove_caption": False,
    }, timeout=timeout)
    messages = sent.get("messages") or []
    out: list[dict] = []
    effective_timeout = timeout if timeout is not None else 1800.0
    for msg in messages:
        out.append(await wait_sent(client, chat_id, msg, effective_timeout))
    return out


async def delete_messages(client: TDClient, chat_id: int,
                          message_ids: list[int], *,
                          revoke: bool = True) -> None:
    await client.request({"@type": "deleteMessages", "chat_id": chat_id,
                          "message_ids": message_ids, "revoke": revoke})


async def edit_message(client: TDClient, chat_id: int, message_id: int,
                       text: str, *,
                       entities: list[dict] | None = None,
                       reply_markup: dict | None = None,
                       timeout: float | None = None) -> dict:
    return await client.request({
        "@type": "editMessageText",
        "chat_id": chat_id,
        "message_id": message_id,
        "reply_markup": reply_markup,
        "input_message_content": {
            "@type": "inputMessageText",
            "text": {"@type": "formattedText", "text": text,
                     "entities": entities or []},
            "link_preview_options": None,
            "clear_draft": False,
        },
    }, timeout=timeout)


async def answer_callback_query(client: TDClient, callback_query_id: int,
                                text: str = "", *, show_alert: bool = False,
                                cache_time: int = 0) -> None:
    await client.request({
        "@type": "answerCallbackQuery",
        "callback_query_id": callback_query_id,
        "text": text,
        "show_alert": show_alert,
        "url": "",
        "cache_time": cache_time,
    })
