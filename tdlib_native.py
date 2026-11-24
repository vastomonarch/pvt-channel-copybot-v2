"""Low-level ctypes binding for the official TDLib JSON interface (tdjson).

Why ctypes and not a pip binding
--------------------------------
* ``ctypes`` is part of the standard library → guaranteed to work on any
  Python version (including 3.14) with no compiled extension of our own.
* We control the whole request/response lifecycle (correlation, timeouts,
  update routing, shutdown) instead of inheriting a binding's event model.
* The native library can come from a system build, a packaging wheel, or an
  explicit path — see :func:`resolve_library_path`.

The per-client handle API (``td_json_client_*``) is used rather than the
global ``td_receive`` API because every client instance then owns an
independent receive queue, which is what we need for running a user account
and a bot account side by side.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import glob
import importlib.util
import json
import logging
import os
import platform
import threading
from pathlib import Path
from typing import Any

log = logging.getLogger("copybot.tdlib.native")

__all__ = [
    "TDLibNotFoundError",
    "NativeTdLib",
    "resolve_library_path",
]


class TDLibNotFoundError(OSError):
    """Raised when no usable libtdjson can be located."""


# ---------------------------------------------------------------------------
# Library resolution
# ---------------------------------------------------------------------------
def _wheel_bundled_candidates() -> list[str]:
    """Locate libtdjson shipped inside popular pip wheels (if installed).

    The ``tdjson`` wheel (any Python version) keeps the shared object in
    ``tdjson.libs/``; the ``python-telegram`` wheel keeps it in
    ``telegram/lib/``.  We only *find* the file here — we never import the
    binding itself.
    """
    out: list[str] = []
    for pkg_name, patterns in (
        ("tdjson", ["../tdjson.libs/libtdjson*.so*", "../tdjson.libs/libtdjson*.dylib*"]),
        ("telegram", ["lib/libtdjson.so", "lib/libtdjson.dylib"]),
    ):
        try:
            spec = importlib.util.find_spec(pkg_name)
        except (ImportError, ValueError):
            spec = None
        if spec is None or not spec.submodule_search_locations:
            continue
        base = Path(list(spec.submodule_search_locations)[0])
        for pattern in patterns:
            out.extend(sorted(str(p) for p in base.glob(pattern)))
    return out


def _local_candidates() -> list[str]:
    root = Path(__file__).resolve().parent
    names = ["libtdjson.so", "libtdjson.dylib", "tdjson.dll"]
    dirs = [
        root / "tdlib" / "lib",
        root / "tdlib" / "bin",
        root,
        Path("/usr/local/lib"),
        Path("/usr/lib"),
        Path("/usr/lib/x86_64-linux-gnu"),
    ]
    return [str(d / n) for d in dirs for n in names if (d / n).exists()]


def resolve_library_path(explicit: str = "") -> str:
    """Find a usable libtdjson, honouring ``TDLIB_LIBRARY_PATH``."""
    tried: list[str] = []

    candidates: list[str] = []
    if explicit:
        candidates.append(explicit)
    env = os.getenv("TDLIB_LIBRARY_PATH", "").strip()
    if env and env not in candidates:
        candidates.append(env)

    system = ctypes.util.find_library("tdjson")
    if system:
        candidates.append(system)

    candidates.extend(_wheel_bundled_candidates())
    candidates.extend(_local_candidates())

    for cand in candidates:
        if not cand:
            continue
        tried.append(cand)
        # find_library may return a bare soname; let CDLL do the final lookup
        if os.path.sep in cand and not os.path.exists(cand):
            continue
        try:
            ctypes.CDLL(cand)
        except OSError:
            continue
        return cand

    raise TDLibNotFoundError(
        "libtdjson (TDLib shared library) not found. Tried:\n  "
        + ("\n  ".join(tried) if tried else "(no candidates)")
        + "\n\nInstall TDLib with one of:\n"
          "  1) pip install tdjson            # prebuilt binary (recommended)\n"
          "  2) build from source:  see docs/INSTALL-TDLIB.md\n"
          "  3) set TDLIB_LIBRARY_PATH=/path/to/libtdjson.so\n"
          f"Platform: {platform.system()} {platform.machine()}"
    )


# ---------------------------------------------------------------------------
# Native wrapper
# ---------------------------------------------------------------------------
class NativeTdLib:
    """Process-wide loader for libtdjson plus per-handle primitives.

    One instance of this class is shared by every :class:`TdClient`; clients
    differ only by their opaque handle.  All methods are thread-safe with
    respect to TDLib's documented guarantees (``send``/``execute`` from any
    thread; ``receive`` from a single thread per handle — enforced by the
    owning client).
    """

    _instance: "NativeTdLib | None" = None
    _instance_lock = threading.Lock()

    def __init__(self, library_path: str = "", *, log_verbosity: int = 1,
                 log_file: str = ""):
        self.library_path = library_path or resolve_library_path()
        self._log_configured = False
        self._load(log_verbosity, log_file)

    # -- process-wide singleton (one CDLL, one native log configuration) --
    @classmethod
    def shared(cls, *, library_path: str = "", log_verbosity: int = 1,
               log_file: str = "") -> "NativeTdLib":
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = cls(library_path,
                                    log_verbosity=log_verbosity,
                                    log_file=log_file)
            return cls._instance

    # -- loading ---------------------------------------------------------
    def _load(self, log_verbosity: int, log_file: str) -> None:
        try:
            self._cdll = ctypes.CDLL(self.library_path)
        except OSError as exc:
            raise TDLibNotFoundError(
                f"Failed to load libtdjson from {self.library_path}: {exc}"
            ) from exc

        c = self._cdll
        # per-client handle API
        c.td_json_client_create.restype = ctypes.c_void_p
        c.td_json_client_create.argtypes = []
        c.td_json_client_send.restype = None
        c.td_json_client_send.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
        c.td_json_client_receive.restype = ctypes.c_char_p   # ctypes copies → safe
        c.td_json_client_receive.argtypes = [ctypes.c_void_p, ctypes.c_double]
        c.td_json_client_destroy.restype = None
        c.td_json_client_destroy.argtypes = [ctypes.c_void_p]

        # global logging configuration (process-wide)
        c.td_set_log_verbosity_level.restype = None
        c.td_set_log_verbosity_level.argtypes = [ctypes.c_int]
        c.td_set_log_file_path.restype = ctypes.c_int
        c.td_set_log_file_path.argtypes = [ctypes.c_char_p]
        c.td_set_log_max_file_size.restype = None
        c.td_set_log_max_file_size.argtypes = [ctypes.c_longlong]
        fatal_cb = ctypes.CFUNCTYPE(None, ctypes.c_char_p)
        c.td_set_log_fatal_error_callback.restype = None
        c.td_set_log_fatal_error_callback.argtypes = [fatal_cb]

        def _on_fatal(msg: bytes) -> None:
            log.critical("TDLib fatal error: %s",
                         msg.decode("utf-8", "replace") if msg else "?")

        self._fatal_cb = fatal_cb(_on_fatal)  # keep a reference (no GC of cfunc)
        c.td_set_log_fatal_error_callback(self._fatal_cb)
        self.configure_log(log_verbosity, log_file)

    def configure_log(self, verbosity: int, log_file: str) -> None:
        """Route TDLib's native log away from stderr into a file."""
        if self._log_configured:
            return
        self._cdll.td_set_log_verbosity_level(int(verbosity))
        if log_file:
            try:
                Path(log_file).parent.mkdir(parents=True, exist_ok=True)
                self._cdll.td_set_log_file_path(log_file.encode())
                # keep the native file from growing without bound (50 MB)
                self._cdll.td_set_log_max_file_size(50 * 1024 * 1024)
            except OSError as exc:  # pragma: no cover - log routing is best effort
                log.warning("could not route TDLib log to %s: %s", log_file, exc)
        self._log_configured = True

    # -- handle lifecycle ------------------------------------------------
    def create_client(self) -> int:
        handle = self._cdll.td_json_client_create()
        if not handle:
            raise RuntimeError("td_json_client_create returned NULL")
        return int(handle)

    def destroy_client(self, handle: int) -> None:
        if handle:
            self._cdll.td_json_client_destroy(ctypes.c_void_p(handle))

    # -- io --------------------------------------------------------------
    def send(self, handle: int, payload: dict | str) -> None:
        """Queue a request.  Safe from any thread (per TDLib docs)."""
        raw = payload if isinstance(payload, str) else json.dumps(
            payload, ensure_ascii=False, separators=(",", ":"))
        self._cdll.td_json_client_send(ctypes.c_void_p(handle),
                                       raw.encode("utf-8"))

    def receive(self, handle: int, timeout: float) -> str | None:
        """Block up to *timeout* seconds for the next payload.

        Must only ever be called from the single receive thread that owns
        *handle* (TDLib requirement).
        """
        raw = self._cdll.td_json_client_receive(ctypes.c_void_p(handle),
                                                float(timeout))
        if raw is None:
            return None
        return raw.decode("utf-8", "replace")
