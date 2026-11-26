import asyncio
import unittest

from errors import TdError, TdTimeoutError
from tdlib_auth import Authenticator
from tdlib_client import TDClient
from tdlib_updates import TrackedTasks, UpdateRouter


class FakeNative:
    def __init__(self):
        self.sent = []
        self._n = 0

    def create_client(self):
        self._n += 1
        return self._n

    def send(self, handle, payload):
        self.sent.append((handle, dict(payload)))

    def receive(self, handle, timeout):
        return None

    def destroy_client(self, handle):
        pass


class RouterTests(unittest.IsolatedAsyncioTestCase):
    async def test_sync_and_async_dispatch(self):
        router = UpdateRouter("t")
        seen = []

        def sync_cb(p):
            seen.append(("sync", p["@type"]))

        async def async_cb(p):
            await asyncio.sleep(0)
            seen.append(("async", p["@type"]))

        router.subscribe(sync_cb, ["updateA"])
        router.subscribe(async_cb, ["updateA"])
        router.dispatch({"@type": "updateA"})
        await asyncio.sleep(0.05)
        self.assertIn(("sync", "updateA"), seen)
        self.assertIn(("async", "updateA"), seen)
        await router.shutdown(timeout=1)

    async def test_exception_isolated(self):
        router = UpdateRouter("t")
        seen = []

        def bad(_):
            raise RuntimeError("boom")

        def good(p):
            seen.append(p["@type"])

        router.subscribe(bad)
        router.subscribe(good)
        router.dispatch({"@type": "updateX"})
        self.assertEqual(seen, ["updateX"])
        await router.shutdown(timeout=1)


class TrackedTasksTests(unittest.IsolatedAsyncioTestCase):
    async def test_drain(self):
        tt = TrackedTasks("x")
        done = asyncio.Event()

        async def work():
            await asyncio.sleep(0.05)
            done.set()

        tt.spawn(work())
        ok = await tt.drain(timeout=1)
        self.assertTrue(ok)
        self.assertTrue(done.is_set())

    async def test_cancel_all(self):
        tt = TrackedTasks("x")

        async def forever():
            await asyncio.sleep(10)

        tt.spawn(forever())
        n = tt.cancel_all()
        self.assertEqual(n, 1)
        await tt.drain(timeout=1)
        self.assertEqual(len(tt), 0)


class TDClientCorrelationTests(unittest.IsolatedAsyncioTestCase):
    async def test_request_correlates_by_extra(self):
        native = FakeNative()
        client = TDClient("u", native)
        client._handle = 1  # bypass start/thread
        task = asyncio.create_task(client.request({"@type": "getMe"}, timeout=1))
        await asyncio.sleep(0)
        self.assertEqual(len(native.sent), 1)
        extra = native.sent[0][1]["@extra"]
        client._on_payload({"@type": "user", "id": 7, "@extra": extra})
        res = await task
        self.assertEqual(res["id"], 7)
        self.assertEqual(client.pending_count, 0)

    async def test_error_response_raises(self):
        native = FakeNative()
        client = TDClient("u", native)
        client._handle = 1
        task = asyncio.create_task(client.request({"@type": "getMe"}, timeout=1))
        await asyncio.sleep(0)
        extra = native.sent[0][1]["@extra"]
        client._on_payload({"@type": "error", "code": 400, "message": "bad", "@extra": extra})
        with self.assertRaises(TdError) as ctx:
            await task
        self.assertEqual(ctx.exception.code, 400)

    async def test_timeout(self):
        native = FakeNative()
        client = TDClient("u", native)
        client._handle = 1
        with self.assertRaises(TdTimeoutError):
            await client.request({"@type": "getMe"}, timeout=0.05)
        self.assertEqual(client.pending_count, 0)

    async def test_multi_client_isolated(self):
        native = FakeNative()
        a = TDClient("a", native)
        b = TDClient("b", native)
        a._handle = 1
        b._handle = 2
        ta = asyncio.create_task(a.request({"@type": "ping"}, timeout=1))
        tb = asyncio.create_task(b.request({"@type": "ping"}, timeout=1))
        await asyncio.sleep(0)
        extra_a = native.sent[0][1]["@extra"]
        extra_b = native.sent[1][1]["@extra"]
        # respond out of order / to wrong client should not cross-wire
        b._on_payload({"@type": "ok", "@extra": extra_b})
        a._on_payload({"@type": "ok", "@extra": extra_a})
        self.assertEqual((await ta)["@type"], "ok")
        self.assertEqual((await tb)["@type"], "ok")


class FakeAuthClient:
    """Minimal client for Authenticator unit tests."""

    def __init__(self):
        self.name = "fake"
        self.authorization_state = "authorizationStateWaitTdlibParameters"
        self.requests = []
        self._waiters = []
        self.router = UpdateRouter("fake")

    async def request(self, payload, timeout=None):
        self.requests.append(dict(payload))
        return {"@type": "ok"}

    def expect(self, types=None, predicate=None):
        # simple waiter that resolves when we call fire()
        fut = asyncio.get_running_loop().create_future()

        class W:
            def __init__(self, f):
                self.f = f
            async def wait(self, timeout=None):
                return await asyncio.wait_for(self.f, timeout)
            def cancel(self):
                if not self.f.done():
                    self.f.cancel()
        w = W(fut)
        self._waiters.append(fut)
        return w

    def fire_state(self, state):
        self.authorization_state = state
        for f in list(self._waiters):
            if not f.done():
                f.set_result({"@type": "updateAuthorizationState",
                              "authorization_state": {"@type": state}})
        self._waiters.clear()


class FakeSession:
    db_dir = "/tmp/fake-db"
    files_dir = "/tmp/fake-files"

    def tdlib_parameters(self, **kw):
        return {"@type": "setTdlibParameters", **kw}


class AuthTests(unittest.IsolatedAsyncioTestCase):
    async def test_bot_token_flow(self):
        client = FakeAuthClient()

        async def prompter(kind, prompt):
            raise AssertionError("prompter should not be called for bot token")

        auth = Authenticator(
            client, FakeSession(), api_id=1, api_hash="h",
            bot_token="123:ABC", prompter=prompter, state_timeout=5,
        )

        async def drive():
            await asyncio.sleep(0.05)
            self.assertEqual(client.requests[0]["@type"], "setTdlibParameters")
            client.fire_state("authorizationStateWaitPhoneNumber")
            await asyncio.sleep(0.05)
            self.assertEqual(client.requests[1]["@type"], "checkAuthenticationBotToken")
            self.assertEqual(client.requests[1]["token"], "123:ABC")
            client.fire_state("authorizationStateReady")

        await asyncio.gather(auth.run(), drive())

    async def test_user_code_password_flow(self):
        client = FakeAuthClient()
        prompts = []

        async def prompter(kind, prompt):
            prompts.append(kind)
            return {"phone": "+10000000000", "code": "12345", "password": "pw"}[kind]

        auth = Authenticator(
            client, FakeSession(), api_id=1, api_hash="h",
            prompter=prompter, state_timeout=5,
        )

        async def drive():
            await asyncio.sleep(0.05)
            client.fire_state("authorizationStateWaitPhoneNumber")
            await asyncio.sleep(0.05)
            client.fire_state("authorizationStateWaitCode")
            await asyncio.sleep(0.05)
            client.fire_state("authorizationStateWaitPassword")
            await asyncio.sleep(0.05)
            client.fire_state("authorizationStateReady")

        await asyncio.gather(auth.run(), drive())
        self.assertEqual(prompts, ["phone", "code", "password"])
        types = [r["@type"] for r in client.requests]
        self.assertIn("setAuthenticationPhoneNumber", types)
        self.assertIn("checkAuthenticationCode", types)
        self.assertIn("checkAuthenticationPassword", types)
        # password/code are sent to TDLib by necessity; ensure we sent them in the right request types
        code_req = [r for r in client.requests if r["@type"] == "checkAuthenticationCode"][0]
        self.assertEqual(code_req["code"], "12345")


if __name__ == "__main__":
    unittest.main()
