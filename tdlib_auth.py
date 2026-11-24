"""TDLib authentication state machine.

Drives TDLib's *own* authorization states — we never imitate Telegram
login by hand:

``waitTdlibParameters → waitPhoneNumber → [waitCode] → [waitPassword]
 → [waitRegistration] → ready``

Two profiles:

* **user account** — phone / code / two-factor password prompted on a TTY
  (via :mod:`getpass` for the password so it is never echoed or logged);
* **bot account** — ``checkAuthenticationBotToken`` straight from
  ``waitPhoneNumber``; no interaction.

Secrets (phone codes, 2FA passwords, bot tokens) are never logged, never
stored by us, and never included in exception messages.
"""

from __future__ import annotations

import asyncio
import getpass
import logging
import os
from typing import Awaitable, Callable

from errors import TdError
from tdlib_client import TDClient
from tdlib_sessions import SessionManager

log = logging.getLogger("copybot.tdlib.auth")

__all__ = [
    "AuthError",
    "AuthInteractiveRequired",
    "Authenticator",
    "default_prompter",
]

Prompter = Callable[[str, str], Awaitable[str]]


class AuthError(RuntimeError):
    """Authorization could not be completed."""


class AuthInteractiveRequired(AuthError):
    """A human must complete login in a terminal (no TTY available)."""


async def default_prompter(kind: str, prompt: str) -> str:
    """Prompt on the controlling terminal (blocking input → worker thread)."""
    if kind == "password":
        def _ask() -> str:
            try:
                return getpass.getpass(prompt)
            except EOFError as exc:
                raise AuthInteractiveRequired(
                    "no terminal available for the 2FA password; run "
                    "`python bot.py --login` in a terminal first"
                ) from exc
        return await asyncio.to_thread(_ask)

    def _ask() -> str:
        try:
            return input(prompt)
        except EOFError as exc:
            raise AuthInteractiveRequired(
                f"no terminal available to enter {kind}; run "
                "`python bot.py --login` in a terminal first"
            ) from exc
    return await asyncio.to_thread(_ask)


class Authenticator:
    """Moves a :class:`TDClient` to ``authorizationStateReady``."""

    WAITING_STATES = {
        "authorizationStateWaitTdlibParameters",
        "authorizationStateWaitPhoneNumber",
        "authorizationStateWaitCode",
        "authorizationStateWaitPassword",
        "authorizationStateWaitRegistration",
    }

    def __init__(self, client: TDClient, session: SessionManager, *,
                 api_id: int, api_hash: str, bot_token: str = "",
                 encryption_key: str = "",
                 first_name: str = "Copy", last_name: str = "",
                 prompter: Prompter | None = None,
                 state_timeout: float = 600.0):
        self.client = client
        self.session = session
        self.api_id = api_id
        self.api_hash = api_hash
        self.bot_token = bot_token
        self.encryption_key = encryption_key
        self.first_name = first_name
        self.last_name = last_name
        self.prompter = prompter or default_prompter
        self.state_timeout = state_timeout

    # ------------------------------------------------------------------
    async def run(self) -> None:
        """Drive the state machine until ready (or raise AuthError)."""
        deadline = asyncio.get_running_loop().time() + self.state_timeout
        acted = False

        while True:
            state = self.client.authorization_state
            if state == "authorizationStateReady":
                log.info("client %s authorized", self.client.name)
                return
            if state in ("authorizationStateClosing",
                         "authorizationStateClosed",
                         "authorizationStateLoggingOut"):
                raise AuthError(
                    f"client {self.client.name} entered {state} during login")

            # Subscribe BEFORE acting so no state change can be missed.
            waiter = self.client.expect(("updateAuthorizationState",))
            try:
                acted = await self._act(state)
            except TdError as exc:
                waiter.cancel()
                raise AuthError(
                    f"{self.client.name}: {state} failed: "
                    f"[{exc.code}] {exc.message}") from exc

            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                waiter.cancel()
                raise AuthError(
                    f"{self.client.name}: login timed out after "
                    f"{self.state_timeout:.0f}s (state={state})")
            try:
                await waiter.wait(timeout=remaining)
            except asyncio.TimeoutError:
                waiter.cancel()
                raise AuthError(
                    f"{self.client.name}: no auth state change for "
                    f"{self.state_timeout:.0f}s (state={state})") from None
            if not acted and state == self.client.authorization_state:
                # Nothing changed and we did nothing — avoid a hot loop.
                await asyncio.sleep(0.1)

    # ------------------------------------------------------------------
    async def _act(self, state: str) -> bool:
        """Perform the action required by *state*. Returns True if we sent
        a request (so a state change is expected)."""
        if state == "authorizationStateWaitTdlibParameters":
            log.info("client %s: sending setTdlibParameters (db=%s)",
                     self.client.name, self.session.db_dir)
            await self.client.request(
                self.session.tdlib_parameters(
                    api_id=self.api_id, api_hash=self.api_hash,
                    encryption_key=self.encryption_key))
            return True

        if state == "authorizationStateWaitPhoneNumber":
            if self.bot_token:
                log.info("client %s: authenticating with bot token "
                         "(token not logged)", self.client.name)
                await self.client.request(
                    {"@type": "checkAuthenticationBotToken",
                     "token": self.bot_token})
                return True
            phone = await self.prompter(
                "phone", f"[{self.client.name}] phone number (+…): ")
            phone = (phone or "").strip()
            if not phone:
                raise AuthInteractiveRequired("empty phone number")
            log.info("client %s: requesting authentication code", self.client.name)
            await self.client.request({
                "@type": "setAuthenticationPhoneNumber",
                "phone_number": phone,
                "settings": None,
            })
            return True

        if state == "authorizationStateWaitCode":
            code = await self.prompter(
                "code", f"[{self.client.name}] authentication code: ")
            code = (code or "").strip()
            if not code:
                raise AuthInteractiveRequired("empty authentication code")
            # Never log the code.
            await self.client.request(
                {"@type": "checkAuthenticationCode", "code": code})
            return True

        if state == "authorizationStateWaitPassword":
            password = await self.prompter(
                "password", f"[{self.client.name}] two-factor password: ")
            if password is None:
                raise AuthInteractiveRequired("empty two-factor password")
            await self.client.request(
                {"@type": "checkAuthenticationPassword",
                 "password": password})
            return True

        if state == "authorizationStateWaitRegistration":
            first = os.getenv("REGISTER_FIRST_NAME", self.first_name) or "Copy"
            last = os.getenv("REGISTER_LAST_NAME", self.last_name) or ""
            log.info("client %s: registering new account", self.client.name)
            await self.client.request({
                "@type": "registerUser",
                "first_name": first,
                "last_name": last,
                "disable_notification": False,
            })
            return True

        if state == "authorizationStateNone":
            # The receive thread has not delivered the first state yet.
            return False

        raise AuthError(
            f"unsupported authorization state {state!r} for "
            f"client {self.client.name}")
