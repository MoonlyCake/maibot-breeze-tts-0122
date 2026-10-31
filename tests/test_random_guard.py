import asyncio
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
import tempfile
import unittest

from voice_guard import finish_random_usage, refund_random_usage, reserve_usage


def random_process_reservation(db_path, number):
    return asyncio.run(reserve_usage(db_path, 'shared-group', 0, 0, str(number),
                                     random_daily_max_requests=5))


class RandomGuardTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / 'guard' / 'usage.sqlite3'

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def reserve(self, group, request, *, limit=5, bypass=False, now=None, global_limit=0):
        return await reserve_usage(self.db, group, 0, global_limit, request,
                                   bypass_limits=bypass, now=now,
                                   random_daily_max_requests=limit)

    async def test_concurrent_random_slots_are_limited_per_group(self):
        results = await asyncio.gather(*(self.reserve('group-1', str(i)) for i in range(30)))
        self.assertEqual(sum(ok for ok, _ in results), 5)
        self.assertEqual(sum(reason == 'random_daily_limit' for _, reason in results), 25)
        self.assertEqual(await self.reserve('group-2', 'other'), (True, 'ok'))
        # Explicit requests do not use the random slots.
        self.assertEqual(await reserve_usage(self.db, 'group-1', 0, 0, 'explicit'), (True, 'ok'))
        # Administrators neither consume nor need a random slot.
        self.assertEqual(await self.reserve('group-1', 'admin', bypass=True), (True, 'ok'))
        self.assertEqual(await self.reserve('group-1', 'still-full'), (False, 'random_daily_limit'))

    async def test_success_finalizes_and_failure_releases_only_once(self):
        self.assertEqual(await self.reserve('g', 'one', limit=1), (True, 'ok'))
        self.assertEqual(await finish_random_usage(self.db, 'g', 'one', success=False), (True, 'ok'))
        self.assertEqual(await finish_random_usage(self.db, 'g', 'one', success=False), (True, 'ok'))
        self.assertEqual(await self.reserve('g', 'two', limit=1), (True, 'ok'))
        self.assertEqual(await finish_random_usage(self.db, 'g', 'two', success=True), (True, 'ok'))
        self.assertEqual(await finish_random_usage(self.db, 'g', 'two', success=False), (True, 'ok'))
        self.assertEqual(await self.reserve('g', 'three', limit=1), (False, 'random_daily_limit'))

    async def test_cross_process_random_slots_are_atomic(self):
        loop = asyncio.get_running_loop()
        with ProcessPoolExecutor(max_workers=4) as pool:
            results = await asyncio.gather(*(loop.run_in_executor(pool, random_process_reservation, str(self.db), i) for i in range(12)))
        self.assertEqual(sum(ok for ok, _ in results), 5)
        self.assertEqual(sum(reason == 'random_daily_limit' for _, reason in results), 7)

    async def test_definite_delivery_failure_refunds_settled_slot_once(self):
        self.assertEqual(await self.reserve('g', 'one', limit=2), (True, 'ok'))
        self.assertEqual(await self.reserve('g', 'two', limit=2), (True, 'ok'))
        # Refund of an in-flight request does not undo its pre-reservation.
        self.assertEqual(await refund_random_usage(self.db, 'g', 'one'), (True, 'ok'))
        self.assertEqual(await self.reserve('g', 'three', limit=2), (False, 'random_daily_limit'))
        self.assertEqual(await finish_random_usage(self.db, 'g', 'one', success=True), (True, 'ok'))
        self.assertEqual(await finish_random_usage(self.db, 'g', 'two', success=True), (True, 'ok'))
        self.assertTrue(all(ok for ok, _ in await asyncio.gather(*(refund_random_usage(self.db, 'g', 'one') for _ in range(20)))))
        self.assertEqual(await self.reserve('g', 'three', limit=2), (True, 'ok'))
        self.assertEqual(await self.reserve('g', 'four', limit=2), (False, 'random_daily_limit'))

    async def test_disabled_counts_and_next_day_resets(self):
        now = datetime.now().astimezone().replace(hour=12)
        self.assertEqual(await self.reserve('g', 'one', limit=0, now=now), (True, 'ok'))
        await finish_random_usage(self.db, 'g', 'one', success=True)
        self.assertEqual(await self.reserve('g', 'two', limit=1, now=now), (False, 'random_daily_limit'))
        self.assertEqual(await self.reserve('g', 'two', limit=1, now=now + timedelta(days=1)), (True, 'ok'))

    async def test_existing_global_limit_remains_independent(self):
        self.assertEqual(await reserve_usage(self.db, 'g1', 0, 1, 'explicit'), (True, 'ok'))
        self.assertEqual(await self.reserve('g2', 'random', global_limit=1), (False, 'daily_limit'))
        self.assertEqual(await self.reserve('g2', 'admin', global_limit=1, bypass=True), (True, 'ok'))

    async def test_invalid_random_reservations_fail_closed(self):
        self.assertEqual(await reserve_usage(self.db, 'g', 0, 0, random_daily_max_requests=5), (False, 'invalid_config'))
        self.assertEqual(await self.reserve('g', 'id', limit=True), (False, 'invalid_config'))
        self.assertEqual(await finish_random_usage(self.db, 'g', 'id', success=1), (False, 'invalid_config'))


if __name__ == '__main__':
    unittest.main()
