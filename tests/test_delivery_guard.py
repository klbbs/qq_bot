import asyncio
from dataclasses import asdict
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src/qq"))
import qq_delivery_guard as module
from qq_delivery_guard import Guard, Policy, SendBlocked, DeliveryUnconfirmed


class Clock:
    def __init__(self):
        self.now = 10000.0

    def __call__(self):
        return self.now

    async def sleep(self, seconds):
        self.now += seconds


class ActionFailed(Exception):
    def __init__(self, message):
        self.info = {"retcode": 1200, "message": message}


class DeliveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "guard.sqlite3"
        self.clock = Clock()
        self.guard = Guard(self.path, clock=self.clock, sleep=self.clock.sleep)
        self.count = 0

    async def send(self):
        self.count += 1
        return {"message_id": self.count}

    async def call(self, text="hello", target=12345, api="send_private_msg"):
        return await self.guard.call("bot", api, {"user_id": target, "message": text}, self.send)

    async def test_duplicate_survives_restart(self):
        first = await self.call()
        self.guard = Guard(self.path, clock=self.clock, sleep=self.clock.sleep)
        self.assertEqual(await self.call(), first)
        self.assertEqual(self.count, 1)

    async def test_distinct_targets_are_not_duplicates(self):
        await self.call(target=12345)
        await self.call(target=67890)
        self.assertEqual(self.count, 2)

    async def test_distinct_bot_is_not_duplicate(self):
        await self.call()
        await self.guard.call("other-bot", "send_private_msg", {"user_id": 12345, "message": "hello"}, self.send)
        self.assertEqual(self.count, 2)

    async def test_send_aliases_share_duplicate_identity(self):
        await self.call()
        await self.call(api="send_private_msg_async")
        await self.call(api="send_private_msg_rate_limited")
        self.assertEqual(self.count, 1)

    async def test_same_target_interval(self):
        await self.call("one")
        await self.call("two")
        self.assertEqual(self.clock.now, 10002)

    async def test_group_interval(self):
        for text in ["one", "two"]:
            await self.guard.call("bot", "send_group_forward_msg", {"group_id": 12345, "messages": text}, self.send)
        self.assertEqual(self.clock.now, 10003)

    async def test_per_minute_global_budget(self):
        self.guard.policy = Policy(global_per_minute=2, max_wait=2)
        await self.call("one")
        await self.call("two", target=67890)
        with self.assertRaises(SendBlocked) as ctx:
            await self.call("three")
        self.assertEqual(ctx.exception.reason, "rate_limit")
        self.assertEqual(self.count, 2)

    async def test_per_chat_budget_does_not_stop_other_chat(self):
        self.guard.policy = Policy(private_per_minute=1, max_wait=2)
        await self.call("one")
        with self.assertRaises(SendBlocked):
            await self.call("two")
        await self.call("three", target=67890)
        self.assertEqual(self.count, 2)

    async def test_network_error_has_no_retry_and_persists(self):
        async def fail():
            self.count += 1
            raise ActionFailed('1006514 网络连接异常! private-secret-text')
        with self.assertRaises(ActionFailed):
            await self.guard.call("bot", "send_private_msg", {"user_id": 12345, "message": "private-secret-text"}, fail)
        self.guard = Guard(self.path, clock=self.clock, sleep=self.clock.sleep)
        self.assertTrue(self.guard.status()["send_paused"])
        with self.assertRaises(SendBlocked):
            await self.call("another")
        self.assertEqual(self.count, 1)
        self.assertNotIn(b"private-secret-text", self.path.read_bytes())

    async def test_rate_restriction_waits_longer(self):
        async def fail():
            raise ActionFailed("发送过于频繁")
        with self.assertRaises(ActionFailed):
            await self.guard.call("bot", "send_group_msg", {"group_id": 12345, "message": "x"}, fail)
        self.assertEqual(self.guard.status()["retry_after"], 600)
        self.assertEqual(self.guard.status()["last_error_kind"], "restricted")

    async def test_unknown_delivery_is_not_resent_after_backoff(self):
        async def fail():
            raise TimeoutError()
        data = {"user_id": 12345, "message": "same"}
        with self.assertRaises(TimeoutError):
            await self.guard.call("bot", "send_private_msg", data, fail)
        self.clock.now += 31
        with self.assertRaises(SendBlocked) as ctx:
            await self.guard.call("bot", "send_private_msg", data, self.send)
        self.assertEqual(ctx.exception.reason, "uncertain_duplicate")
        self.assertEqual(self.count, 0)

    async def test_missing_receipt_is_degraded(self):
        async def missing():
            return {"ok": True}
        with self.assertRaises(DeliveryUnconfirmed):
            await self.guard.call("bot", "send_private_msg", {"user_id": 12345}, missing)
        self.assertEqual(self.guard.status()["delivery_state"], "degraded")

    async def test_health_does_not_claim_delivery_before_first_send(self):
        self.assertEqual(self.guard.status()["delivery_state"], "unverified")
        await self.call()
        self.assertEqual(self.guard.status()["delivery_state"], "confirmed")

    async def test_read_calls_remain_available_during_pause(self):
        with self.guard.db() as db:
            self.guard.put(db, "paused_until", self.clock.now + 1000)
        await self.guard.call("bot", "get_friend_msg_history", {}, self.send)
        self.assertEqual(self.count, 1)

    async def test_cancelled_network_call_keeps_uncertain_record(self):
        async def cancelled():
            raise asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.guard.call("bot", "send_private_msg", {"user_id": 12345}, cancelled)
        self.assertEqual(self.guard.pending, 0)
        self.assertFalse(self.guard.lock.locked())
        self.assertTrue(self.guard.status()["send_paused"])

    async def test_queue_is_bounded(self):
        self.guard.policy = Policy(max_pending=1)
        entered, release = asyncio.Event(), asyncio.Event()
        async def slow():
            entered.set()
            await release.wait()
            return {"message_id": 1}
        task = asyncio.create_task(self.guard.call("bot", "send_private_msg", {"user_id": 12345}, slow))
        await entered.wait()
        try:
            with self.assertRaises(SendBlocked) as ctx:
                await self.call("other")
            self.assertEqual(ctx.exception.reason, "queue_full")
        finally:
            release.set()
            await task

    async def test_waiter_cancellation_releases_pending_count(self):
        await self.guard.lock.acquire()
        task = asyncio.create_task(self.call())
        await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.guard.pending, 0)
        self.guard.lock.release()

    async def test_concurrent_duplicate_sends_once(self):
        results = await asyncio.gather(*[self.call() for _ in range(10)])
        self.assertEqual(self.count, 1)
        self.assertEqual(len({r["message_id"] for r in results}), 1)

    async def test_success_after_pause_recovers_health(self):
        async def fail():
            raise TimeoutError()
        with self.assertRaises(TimeoutError):
            await self.guard.call("bot", "send_private_msg", {"user_id": 12345}, fail)
        self.clock.now += 31
        await self.call("new")
        self.assertEqual(self.guard.status()["delivery_state"], "confirmed")


class PolicyTests(unittest.TestCase):
    def test_default_merges_split_and_disables_auto_friend(self):
        self.assertEqual(module.collapse_text("a<SPLIT>b<SPLIT>c"), "a\n\nb\n\nc")
        self.assertFalse(Policy().friend_auto_accept)

    def test_bad_policies_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "policy.json"
            for data in [{"enabled": "false"}, {"max_pending": 0}, {"max_wait": float("nan")}, {"global_per_minute": True}, {"unknown": 1}]:
                path.write_text(json.dumps(data))
                with self.assertRaises(ValueError):
                    Policy.load(path)

    def test_default_policy_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "policy.json"
            path.write_text(json.dumps(asdict(Policy())))
            self.assertEqual(Policy.load(path), Policy())


if __name__ == "__main__":
    unittest.main()
