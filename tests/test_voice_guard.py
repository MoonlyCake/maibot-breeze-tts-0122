import asyncio
from concurrent.futures import ProcessPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest

from voice_guard import claim_request, is_admin, reserve_usage, scope_allowed


def process_reservation(db_path, number):
    return asyncio.run(reserve_usage(db_path, f"process-{number}", 0, 3, str(number)))


def process_admin_reservation(db_path):
    return asyncio.run(reserve_usage(db_path, "qq:group", 60, 1, "same-admin-event", bypass_limits=True))


class VoiceGuardTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "guard" / "usage.sqlite3"

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def test_scope_is_fail_closed(self):
        self.assertFalse(scope_allowed(True, "123", [], False, platform="qq"))
        self.assertTrue(scope_allowed(True, "123", ["123"], False, platform="qq"))
        self.assertTrue(scope_allowed(True, "123", ["qq:123"], False, platform="qq"))
        self.assertFalse(scope_allowed(True, "123", ["123"], False, platform="other"))
        self.assertFalse(scope_allowed(True, "123", ["qq:123"], False, platform="other"))
        self.assertTrue(scope_allowed(True, "123", ["other:123"], False, platform="other"))
        self.assertFalse(scope_allowed(True, None, ["123"], True, platform="qq"))
        self.assertFalse(scope_allowed(None, "123", ["123"], True, platform="qq"))
        self.assertFalse(scope_allowed(False, None, [], False, platform="qq"))
        self.assertTrue(scope_allowed(False, None, [], True, platform="qq"))
        self.assertFalse(scope_allowed(False, None, [], True, platform="unknown"))
        self.assertFalse(scope_allowed(False, None, [], True, platform=""))
        self.assertFalse(scope_allowed(True, "123", "123", False, platform="qq"))

    def test_admin_requires_exact_platform_and_sender(self):
        self.assertTrue(is_admin("qq", "123", ["123"]))
        self.assertTrue(is_admin(" QQ ", 123, [" qq:123 "]))
        self.assertFalse(is_admin("telegram", "123", ["123"]))
        self.assertFalse(is_admin("telegram", "123", ["qq:123"]))
        self.assertTrue(is_admin("telegram", "123", ["telegram:123"]))
        self.assertFalse(is_admin("unknown", "123", ["unknown:123"]))
        self.assertFalse(is_admin("", "123", ["123"]))
        self.assertFalse(is_admin("qq", None, ["123"]))
        self.assertFalse(is_admin("qq", True, ["qq:True"]))
        self.assertFalse(is_admin("qq", "123:extra", ["qq:123:extra"]))
        self.assertFalse(is_admin("qq", "123", "123"))
        self.assertFalse(is_admin("qq", "123", []))

    async def test_admin_bypasses_limits_without_affecting_normal_usage(self):
        day = datetime.now().astimezone()
        self.assertEqual(await reserve_usage(self.db, "qq:group", 60, 1, "normal-1", now=day), (True, "ok"))
        self.assertEqual(await reserve_usage(self.db, "qq:group", 60, 1, "admin-1", now=day + timedelta(seconds=1), bypass_limits=True), (True, "ok"))
        self.assertEqual(await reserve_usage(self.db, "qq:group", 60, 1, "admin-1", now=day + timedelta(seconds=2), bypass_limits=True), (False, "duplicate"))
        with closing(sqlite3.connect(str(self.db))) as db:
            self.assertEqual(db.execute("SELECT attempts FROM daily_usage").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT last_attempt FROM cooldowns").fetchone()[0], day.timestamp())
        self.assertEqual(await reserve_usage(self.db, "qq:group", 60, 2, "normal-2", now=day + timedelta(seconds=59)), (False, "cooldown"))
        self.assertEqual(await reserve_usage(self.db, "qq:group", 60, 2, "normal-2", now=day + timedelta(seconds=61)), (True, "ok"))

    async def test_admin_calls_are_concurrent_and_still_deduplicated(self):
        result = await asyncio.gather(*(reserve_usage(self.db, "qq:group", 60, 1, "same", bypass_limits=True) for _ in range(20)))
        self.assertEqual(sum(allowed for allowed, _ in result), 1)
        self.assertEqual(sum(reason == "duplicate" for _, reason in result), 19)
        result = await asyncio.gather(*(reserve_usage(self.db, f"qq:group-{i}", 60, 1, f"admin-{i}", bypass_limits=True) for i in range(10)))
        self.assertTrue(all(allowed for allowed, _ in result))
        self.assertEqual(await reserve_usage(self.db, "qq:group", 0, 1, "normal"), (True, "ok"))
        self.assertEqual(await reserve_usage(self.db, "qq:another", 0, 1, "next"), (False, "daily_limit"))

    async def test_admin_exemption_rejects_invalid_input_and_storage(self):
        self.assertEqual(await reserve_usage(self.db, "session", 1, 1, "id", bypass_limits=1), (False, "invalid_config"))
        self.assertEqual(await reserve_usage(self.db, "session", 1, 1, bypass_limits=True), (False, "invalid_config"))
        self.assertEqual(await reserve_usage(self.db, "session", 1, 1, "", bypass_limits=True), (False, "invalid_config"))
        self.assertEqual(await reserve_usage(self.db, "session", -1, 1, "id", bypass_limits=True), (False, "invalid_config"))
        blocker = Path(self.tmp.name) / "not-a-directory"
        blocker.write_text("test")
        self.assertEqual(await reserve_usage(blocker / "x.sqlite", "session", 0, 1, "id", bypass_limits=True), (False, "storage_error"))

    async def test_admin_dedup_is_shared_across_processes(self):
        loop = asyncio.get_running_loop()
        with ProcessPoolExecutor(max_workers=4) as pool:
            result = await asyncio.gather(*(loop.run_in_executor(pool, process_admin_reservation, str(self.db)) for _ in range(12)))
        self.assertEqual(sum(allowed for allowed, _ in result), 1)
        self.assertEqual(sum(reason == "duplicate" for _, reason in result), 11)

    async def test_legacy_text_claim_and_admin_voice_reservation_use_separate_ids(self):
        self.assertEqual(await claim_request(self.db, "qq:group", "message-1"), (True, "ok"))
        self.assertEqual(await reserve_usage(self.db, "qq:group", 60, 1, "voice:message-1", bypass_limits=True), (True, "ok"))
        self.assertEqual(await claim_request(self.db, "qq:group", "message-1"), (False, "duplicate"))
        self.assertEqual(await reserve_usage(self.db, "qq:group", 60, 1, "voice:message-1", bypass_limits=True), (False, "duplicate"))

    async def test_racing_requests_reserve_only_one_per_cooldown(self):
        results = await asyncio.gather(*(reserve_usage(self.db, "session-one", 60, 10, str(i)) for i in range(20)))
        self.assertEqual(sum(allowed for allowed, _ in results), 1)
        self.assertEqual(sum(reason == "cooldown" for _, reason in results), 19)

    async def test_daily_cap_is_global_and_atomic(self):
        results = await asyncio.gather(*(reserve_usage(self.db, f"session-{i}", 0, 10, str(i)) for i in range(30)))
        self.assertEqual(sum(allowed for allowed, _ in results), 10)
        self.assertEqual(sum(reason == "daily_limit" for _, reason in results), 20)

    async def test_cross_process_transactions(self):
        loop = asyncio.get_running_loop()
        with ProcessPoolExecutor(max_workers=4) as pool:
            results = await asyncio.gather(*(loop.run_in_executor(pool, process_reservation, str(self.db), i) for i in range(12)))
        self.assertEqual(sum(allowed for allowed, _ in results), 3)
        self.assertEqual(sum(reason == "daily_limit" for _, reason in results), 9)

    async def test_deduplication_and_daily_reset_survive_new_connections(self):
        day1 = datetime.now().astimezone().replace(hour=12, minute=0, second=0, microsecond=0)
        day2 = day1 + timedelta(days=1)
        self.assertEqual(await reserve_usage(self.db, "group-123", 60, 1, "event-1", now=day1), (True, "ok"))
        self.assertEqual(await reserve_usage(self.db, "group-123", 60, 1, "event-1", now=day1 + timedelta(seconds=61)), (False, "duplicate"))
        self.assertEqual(await reserve_usage(self.db, "group-456", 60, 1, "event-2", now=day1), (False, "daily_limit"))
        self.assertEqual(await reserve_usage(self.db, "group-123", 60, 1, "event-1", now=day2), (False, "duplicate"))
        self.assertEqual(await reserve_usage(self.db, "group-123", 60, 1, "event-3", now=day2), (True, "ok"))

    async def test_request_id_is_accepted_after_seven_day_retention(self):
        day = datetime.now().astimezone()
        self.assertEqual(await reserve_usage(self.db, "group-123", 0, 10, "event-1", now=day), (True, "ok"))
        # Same event is the first operation after expiration; no other request
        # should be needed to clean up stale deduplication entries.
        self.assertEqual(await reserve_usage(self.db, "group-123", 0, 10, "event-1", now=day + timedelta(days=8)), (True, "ok"))

    async def test_atomic_claim_blocks_duplicate_fallbacks_without_using_quota(self):
        claims = await asyncio.gather(*(claim_request(self.db, "group-123", "event-1") for _ in range(20)))
        self.assertEqual(sum(allowed for allowed, _ in claims), 1)
        self.assertEqual(sum(reason == "duplicate" for _, reason in claims), 19)
        # Claim and quota are separate: only an actual provider attempt counts.
        self.assertEqual(await reserve_usage(self.db, "group-123", 0, 1), (True, "ok"))
        self.assertEqual(await reserve_usage(self.db, "group-123", 0, 1), (False, "daily_limit"))

    async def test_claim_expires_and_storage_failure_is_closed(self):
        day = datetime.now().astimezone()
        self.assertEqual(await claim_request(self.db, "group-123", "event-1", now=day), (True, "ok"))
        self.assertEqual(await claim_request(self.db, "group-123", "event-1", now=day + timedelta(days=8)), (True, "ok"))
        self.assertEqual(await claim_request(self.db, "group-123", "", now=day), (False, "invalid_config"))
        blocker = Path(self.tmp.name) / "not-a-directory"
        blocker.write_text("test")
        self.assertEqual(await claim_request(blocker / "x.sqlite", "session", "event"), (False, "storage_error"))

    async def test_rejected_requests_are_not_recorded_and_private_identifiers_are_hashed(self):
        now = datetime.now().astimezone()
        self.assertEqual(await reserve_usage(self.db, "private-session-value", 60, 10, "secret-message-id", now=now), (True, "ok"))
        self.assertEqual(await reserve_usage(self.db, "private-session-value", 60, 10, "rejected-event", now=now), (False, "cooldown"))
        with closing(sqlite3.connect(str(self.db))) as db:
            self.assertEqual(db.execute("SELECT attempts FROM daily_usage").fetchone()[0], 1)
            all_values = repr(list(db.iterdump()))
        self.assertNotIn("private-session-value", all_values)
        self.assertNotIn("secret-message-id", all_values)
        self.assertNotIn("rejected-event", all_values)
        self.assertEqual(os.stat(self.db).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(self.db.parent).st_mode & 0o777, 0o700)

    async def test_unlimited_still_counts_for_later_cap(self):
        for i in range(5):
            self.assertEqual(await reserve_usage(self.db, "session", 0, 0, str(i)), (True, "ok"))
        self.assertEqual(await reserve_usage(self.db, "session", 0, 5), (False, "daily_limit"))
        self.assertEqual(await reserve_usage(self.db, "session", 0, 6), (True, "ok"))
        self.assertEqual(await reserve_usage(self.db, "session", 0, 6), (False, "daily_limit"))

    async def test_fail_closed_invalid_storage_or_settings(self):
        self.assertEqual(await reserve_usage(self.db, "session", -1, 10), (False, "invalid_config"))
        self.assertEqual(await reserve_usage(self.db, "session", 0, True), (False, "invalid_config"))
        self.assertEqual(await reserve_usage(self.db, "session", 0, 10, ""), (False, "invalid_config"))
        blocker = Path(self.tmp.name) / "not-a-directory"
        blocker.write_text("test")
        self.assertEqual(await reserve_usage(blocker / "x.sqlite", "session", 0, 10), (False, "storage_error"))


if __name__ == "__main__":
    unittest.main()
