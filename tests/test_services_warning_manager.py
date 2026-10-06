import asyncio
import unittest
from unittest import mock

from models import warning_manager as wm_module
from models.warning_manager import WarningManager


def make_manager():
    manager = WarningManager.__new__(WarningManager)
    manager.worksheet = mock.Mock()
    manager.worksheet.get_all_records.side_effect = RuntimeError('quota')
    manager._warnings_cache = None
    manager._cache_timestamp = None
    manager._cache_ttl = 300
    manager._lookup_failure_notified_at = None
    manager._notify_log_channel = mock.AsyncMock()
    return manager


class SheetLookupFailureTest(unittest.IsolatedAsyncioTestCase):
    async def test_failure_without_cache_notifies_once(self):
        manager = make_manager()
        client = mock.MagicMock()
        client.loop = asyncio.get_running_loop()

        with mock.patch.object(wm_module, 'BotManager') as bm, \
             self.assertLogs('scrim-bot.warning_manager', level='ERROR'):
            bm.get_instance.return_value.get_client.return_value = client
            first = await asyncio.to_thread(manager.is_restricted, '1', 'a')
            second = await asyncio.to_thread(manager.is_restricted, '2', 'b')
            for _ in range(5):
                await asyncio.sleep(0)

        self.assertEqual(first, (False, None))
        self.assertEqual(second, (False, None))
        manager._notify_log_channel.assert_awaited_once()
        self.assertIn('경고 시트 조회에 실패', manager._notify_log_channel.await_args.args[0])

    async def test_failure_with_cache_does_not_notify(self):
        manager = make_manager()
        manager._warnings_cache = []
        with mock.patch.object(wm_module, 'BotManager'), \
             self.assertLogs('scrim-bot.warning_manager', level='ERROR'):
            result = await asyncio.to_thread(manager.is_restricted, '1', 'a')
        self.assertEqual(result, (False, None))
        manager._notify_log_channel.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
