import random
import unittest

from errors import ErrorKind, TdError, backoff_delay, classify, is_message_not_modified


class TestClassify(unittest.TestCase):
    def test_flood_wait_parsed(self):
        # FLOOD_WAIT_N gets +2s buffer in classifier
        c = classify(TdError(420, "FLOOD_WAIT_5"))
        self.assertEqual(c.kind, ErrorKind.FLOOD_WAIT)
        self.assertEqual(c.wait_seconds, 7.0)
        self.assertTrue(c.retryable)

    def test_flood_wait_429_fallback(self):
        c = classify(TdError(429, "Too Many Requests: retry after 17"))
        self.assertEqual(c.kind, ErrorKind.FLOOD_WAIT)
        self.assertEqual(c.wait_seconds, 5.0)  # generic 429 fallback
        self.assertTrue(c.retryable)

    def test_retryable_internal(self):
        c = classify(TdError(500, "Failed to get response"))
        self.assertEqual(c.kind, ErrorKind.TDLIB_INTERNAL)
        self.assertTrue(c.retryable)
        self.assertFalse(c.stop_job)

    def test_message_not_found_skipped(self):
        c = classify(TdError(404, "MESSAGE_NOT_FOUND"))
        self.assertEqual(c.kind, ErrorKind.INVALID_MESSAGE)
        self.assertTrue(c.skip_message)
        self.assertFalse(c.stop_job)

    def test_auth(self):
        c = classify(TdError(401, "Unauthorized"))
        self.assertEqual(c.kind, ErrorKind.AUTH)
        self.assertTrue(c.stop_app)

    def test_permission(self):
        c = classify(TdError(403, "CHAT_WRITE_FORBIDDEN"))
        self.assertEqual(c.kind, ErrorKind.PERMISSION)
        self.assertFalse(c.retryable)

    def test_message_not_modified(self):
        self.assertTrue(is_message_not_modified(TdError(400, "MESSAGE_NOT_MODIFIED")))
        self.assertFalse(is_message_not_modified(TdError(400, "Something else")))
        self.assertTrue(is_message_not_modified("400: MESSAGE_NOT_MODIFIED"))


class TestBackoff(unittest.TestCase):
    def test_bounds(self):
        # jitter applies around nominal (nominal capped at 10); result can exceed
        # nominal cap slightly due to +jitter, so just assert sane positive bound.
        rng = random.Random(0)
        for attempt in range(0, 10):
            d = backoff_delay(attempt, base=1.0, factor=2.0, cap=10.0, rng=rng)
            self.assertGreaterEqual(d, 0.0)
            self.assertLessEqual(d, 12.5)

    def test_grows_without_jitter(self):
        rng = random.Random(1)
        d0 = backoff_delay(0, base=1.0, factor=2.0, cap=100.0, jitter=0.0, rng=rng)
        d3 = backoff_delay(3, base=1.0, factor=2.0, cap=100.0, jitter=0.0, rng=rng)
        self.assertEqual(d0, 1.0)
        self.assertEqual(d3, 8.0)


if __name__ == "__main__":
    unittest.main()
