"""Error classification and retry policy for TDLib / Telegram failures.

Every failure that can surface from the TDLib layer (or from local I/O) is
mapped onto an :class:`ErrorKind` plus a small set of action flags that the
pipeline and job manager can act on:

* ``retryable``   — safe to retry with exponential backoff + jitter
* ``wait_seconds``— for flood waits, how long Telegram asked us to sleep
* ``needs_refetch`` — the file reference / message may be stale, re-fetch first
* ``skip_message``— this particular message is unusable; skip it and continue
* ``stop_job``    — the job cannot proceed; fail it with the reason
* ``stop_app``    — the client is no longer usable (auth lost); shut down

The classifier accepts either a TDLib error dict (``{"@type": "error", ...}``)
or a Python exception.
"""

from __future__ import annotations

import asyncio
import random
import re
from dataclasses import dataclass
from enum import Enum

__all__ = [
    "ErrorKind",
    "ClassifiedError",
    "TdError",
    "TdTimeoutError",
    "TdClientClosedError",
    "classify",
    "backoff_delay",
    "is_message_not_modified",
]


class ErrorKind(str, Enum):
    FLOOD_WAIT = "flood_wait"
    NETWORK = "network"                  # transient connectivity / timeout
    TDLIB_INTERNAL = "tdlib_internal"    # server-side 5xx / aborted request
    AUTH = "auth"                        # authorization lost or invalid
    PERMISSION = "permission"            # access rights problem
    INVALID_MESSAGE = "invalid_message"  # message deleted / id invalid
    MISSING_MEDIA = "missing_media"      # media empty, revoked, ref expired
    DISK = "disk"                        # local filesystem failure
    NOT_MODIFIED = "not_modified"        # benign (edit was a no-op)
    PERMANENT = "permanent"              # bad request; retrying won't help
    UNKNOWN = "unknown"


@dataclass
class ClassifiedError:
    kind: ErrorKind
    message: str
    code: int = 0
    wait_seconds: float = 0.0
    retryable: bool = False
    needs_refetch: bool = False
    skip_message: bool = False
    stop_job: bool = False
    stop_app: bool = False
    raw: object = None

    def __str__(self) -> str:  # pragma: no cover - debug aid
        parts = [self.kind.value]
        if self.code:
            parts.append(f"code={self.code}")
        if self.wait_seconds:
            parts.append(f"wait={self.wait_seconds:.0f}s")
        parts.append(self.message)
        return " ".join(parts)


# ---------------------------------------------------------------------------
# Exceptions raised by our own TDLib layer
# ---------------------------------------------------------------------------
class TdError(Exception):
    """A TDLib ``error`` object turned into an exception."""

    def __init__(self, code: int, message: str):
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message

    def as_dict(self) -> dict:
        return {"@type": "error", "code": self.code, "message": self.message}


class TdTimeoutError(TimeoutError):
    """A request did not receive its correlated response in time."""


class TdClientClosedError(RuntimeError):
    """A request was attempted on a closed / closing TDLib client."""


# ---------------------------------------------------------------------------
# Patterns (order matters: first match wins)
# ---------------------------------------------------------------------------
_FLOOD_RE = re.compile(r"FLOOD_WAIT[_ ]?(\d+)", re.IGNORECASE)
_TOO_MANY_RE = re.compile(r"TOO_MANY_REQUESTS|RATE_LIMIT", re.IGNORECASE)
_REF_RE = re.compile(r"FILE_REFERENCE_\w+_EXPIRED|FILE_REFERENCE_EXPIRED|"
                     r"FILE_REFERENCE_INVALID|FILE_REFERENCE_(\w+)", re.IGNORECASE)
_MEDIA_RE = re.compile(r"\bMEDIA_EMPTY\b|\bFILE_EXPIRED\b|\bFILE_DOWNLOAD_"
                       r"RETRY_ERROR\b|\bMEDIA_INVALID\b|\bMEDIA_TYPE_INVALID\b",
                       re.IGNORECASE)
_INVALID_MSG_RE = re.compile(r"MESSAGE_ID_INVALID|MESSAGE_NOT_FOUND|\b404\b|"
                             r"MESSAGE_DELETED|CHAT_NOT_MODIFIED", re.IGNORECASE)
_NOT_MODIFIED_RE = re.compile(r"MESSAGE_NOT_MODIFIED|MESSAGE_EMPTY", re.IGNORECASE)
_PERMISSION_RE = re.compile(r"CHAT_ADMIN_REQUIRED|CHANNEL_PRIVATE|"
                            r"MESSAGE_DELETE_FORBIDDEN|CHAT_WRITE_FORBIDDEN|"
                            r"USER_BANNED_IN_CHANNEL|PEER_ID_INVALID|"
                            r"CHAT_PEERS_TIMEOUT|RIGHTS_REQUIRED|"
                            r"CHAT_ADMIN_INVALID|FORBIDDEN|"
                            r"CHAT_FORBIDDEN|USER_NOT_PARTICIPANT",
                            re.IGNORECASE)
_AUTH_RE = re.compile(r"\b401\b|AUTH_|SESSION_|USER_DEACTIVATED|"
                      r"AUTH_KEY|SESSION_PASSWORD_NEEDED|LOGIN_REQUIRED|"
                      r"AUTHORIZATION_FAILED|USER_DEACTIVATED_BAN", re.IGNORECASE)
_NET_RE = re.compile(r"\bNET\b|CONNECTION|TIMEOUT|TIMEOUT_ERROR|"
                     r"FAILED_TO_CONNECT|CONNECTION_RESET|"
                     r"REQUEST_TIMEOUT|NETWORK_MISDELIVERY|"
                     r"PROXY_|DNS_|SOCKET", re.IGNORECASE)


def is_message_not_modified(err: object) -> bool:
    """True for the benign 'edit changed nothing' TDLib error."""
    msg = _message_of(err)
    return bool(_NOT_MODIFIED_RE.search(msg))


def _message_of(err: object) -> str:
    if isinstance(err, TdError):
        return err.message
    if isinstance(err, dict):
        return str(err.get("message", ""))
    if isinstance(err, BaseException):
        return str(err)
    return str(err)


def _code_of(err: object) -> int:
    if isinstance(err, TdError):
        return err.code
    if isinstance(err, dict):
        try:
            return int(err.get("code", 0))
        except (TypeError, ValueError):
            return 0
    return 0


def classify(err: object) -> ClassifiedError:
    """Map a TDLib error object or Python exception to a ClassifiedError."""
    # --- Python exceptions ------------------------------------------------
    if isinstance(err, asyncio.CancelledError):  # never classify cancellation
        raise err
    if isinstance(err, TdTimeoutError):
        return ClassifiedError(ErrorKind.NETWORK, "request timed out",
                               retryable=True, raw=err)
    if isinstance(err, TdClientClosedError):
        return ClassifiedError(ErrorKind.AUTH, str(err), stop_app=True, raw=err)
    if isinstance(err, (ConnectionError, TimeoutError)):
        return ClassifiedError(ErrorKind.NETWORK, str(err) or type(err).__name__,
                               retryable=True, raw=err)
    if isinstance(err, OSError):
        # Local disk / IO problems: ENOSPC, EIO, EDQUOT, …
        return ClassifiedError(ErrorKind.DISK, f"{type(err).__name__}: {err}",
                               stop_job=True, raw=err)

    code = _code_of(err)
    message = _message_of(err)

    # --- TDLib dict / exception ------------------------------------------
    if code == 401 or _AUTH_RE.search(message):
        return ClassifiedError(ErrorKind.AUTH, message, code=code,
                               stop_app=True, raw=err)

    m = _FLOOD_RE.search(message)
    if m:
        wait = float(m.group(1)) + 2.0  # 2s buffer, as in the old code
        return ClassifiedError(ErrorKind.FLOOD_WAIT, message, code=code or 429,
                               wait_seconds=wait, retryable=True, raw=err)
    if code == 429 or _TOO_MANY_RE.search(message):
        return ClassifiedError(ErrorKind.FLOOD_WAIT, message, code=429,
                               wait_seconds=5.0, retryable=True, raw=err)

    if _NOT_MODIFIED_RE.search(message):
        return ClassifiedError(ErrorKind.NOT_MODIFIED, message, code=code, raw=err)

    if _REF_RE.search(message) or _MEDIA_RE.search(message):
        return ClassifiedError(ErrorKind.MISSING_MEDIA, message, code=code,
                               retryable=True, needs_refetch=True, raw=err)

    if _PERMISSION_RE.search(message):
        return ClassifiedError(ErrorKind.PERMISSION, message, code=code,
                               raw=err)

    if _INVALID_MSG_RE.search(message):
        return ClassifiedError(ErrorKind.INVALID_MESSAGE, message, code=code,
                               skip_message=True, raw=err)

    if code == 500 or re.search(r"INTERNAL|Request aborted|Failed to fetch",
                                message, re.IGNORECASE):
        return ClassifiedError(ErrorKind.TDLIB_INTERNAL, message, code=code,
                               retryable=True, raw=err)

    if _NET_RE.search(message):
        return ClassifiedError(ErrorKind.NETWORK, message, code=code,
                               retryable=True, raw=err)

    if code == 400 or code == 404:
        # A genuine bad request: skipping the message is the least destructive
        # response for a copy pipeline; callers may still stop the job.
        return ClassifiedError(ErrorKind.PERMANENT, message, code=code,
                               skip_message=True, raw=err)

    if code:
        return ClassifiedError(ErrorKind.UNKNOWN, message, code=code,
                               retryable=True, raw=err)

    return ClassifiedError(ErrorKind.UNKNOWN, message or repr(err),
                           retryable=False, raw=err)


def backoff_delay(attempt: int, *, base: float = 1.0, factor: float = 2.0,
                  cap: float = 60.0, jitter: float = 0.25,
                  rng: random.Random | None = None) -> float:
    """Exponential backoff with symmetric jitter.

    ``attempt`` is 0-based.  With ``jitter=0.25`` the delay is the nominal
    exponential value ±25 %, which de-synchronises concurrent retries.
    Deterministic when *rng* is supplied (used by tests).
    """
    if attempt < 0:
        attempt = 0
    nominal = min(cap, base * (factor ** attempt))
    if jitter <= 0:
        return nominal
    r = rng if rng is not None else random
    return max(0.0, nominal * (1.0 + jitter * (r.random() * 2.0 - 1.0)))
