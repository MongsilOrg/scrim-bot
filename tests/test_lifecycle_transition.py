import asyncio
import unittest
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

from config.settings import settings
from models import scrim_orchestrator
from utils.helpers import KST


def _kst(hour, minute=0, second=0):
    return datetime(2026, 10, 6, hour, minute, second, tzinfo=KST)


class TransitionDashboardFailureTest(unittest.IsolatedAsyncioTestCase):
    async def test_background_tasks_start_even_if_dashboard_fails(self):
        calls = MagicMock()
        old_tdm = MagicMock(mmr_message=None, mmr_message_id=None, dashboard_message_id=55)
        new_tdm = MagicMock(teams={})
        new_tdm.initialize_new_scrim = AsyncMock()
        new_tdm.start_background_tasks = calls.start
        new_tdm.save_backup = calls.save
        refresh = AsyncMock(side_effect=RuntimeError("dashboard down"))

        async def _refresh(channel):
            calls.refresh()
            await refresh(channel)

        with patch.object(scrim_orchestrator, 'BotManager') as BM:
            manager = BM.get_instance.return_value
            manager.get_team_data_manager.return_value = old_tdm
            manager.reset_team_data_manager = AsyncMock(return_value=new_tdm)
            await scrim_orchestrator.transition_to_next_scrim(MagicMock(), MagicMock(), _refresh)

        names = [c[0] for c in calls.mock_calls]
        self.assertEqual(names[:3], ['start', 'refresh', 'save'])
        self.assertEqual(new_tdm.dashboard_message_id, 55)


class DailyResetLoopTest(unittest.IsolatedAsyncioTestCase):
    def _client(self, iterations=1, guild=True):
        client = MagicMock()
        client.wait_until_ready = AsyncMock()
        client.is_closed.side_effect = [False] * iterations + [True]
        client.guilds = []
        if guild:
            client.get_guild.return_value.get_channel.return_value = MagicMock()
        else:
            client.get_guild.return_value = None
        return client

    async def _run(self, client, now, expired):
        sleep = AsyncMock()
        transition = AsyncMock()
        with patch.object(scrim_orchestrator, 'BotManager'), \
             patch.object(scrim_orchestrator, 'is_scrim_expired', side_effect=expired), \
             patch.object(scrim_orchestrator, 'transition_to_next_scrim', transition), \
             patch.object(scrim_orchestrator, 'get_current_kst_time', return_value=now), \
             patch.object(scrim_orchestrator.asyncio, 'sleep', sleep):
            await scrim_orchestrator.daily_reset_loop(client, AsyncMock())
        return transition, sleep

    async def test_transitions_at_once_when_already_expired(self):
        client = self._client()
        transition, _ = await self._run(client, _kst(22, 0, 30), [True])
        transition.assert_awaited_once()
        client.get_guild.assert_called_with(settings.GUILD_ID)

    async def test_waits_until_22_when_not_expired(self):
        transition, sleep = await self._run(self._client(), _kst(21, 59, 30), [False])
        transition.assert_not_awaited()
        self.assertEqual(sleep.await_args.args[0], 31)

    async def test_missing_guild_retries_and_logs_error_once(self):
        client = self._client(iterations=2, guild=False)
        with self.assertLogs('scrim-bot.scrim_orchestrator', level='WARNING') as logs:
            transition, sleep = await self._run(client, _kst(22, 1), [True, True])
        transition.assert_not_awaited()
        levels = [r.levelname for r in logs.records if '자동 전환 건너뜀' in r.getMessage()]
        self.assertEqual(levels, ['ERROR', 'WARNING'])
        self.assertEqual(sleep.await_args.args[0], scrim_orchestrator.RESET_RETRY_SECONDS)


class SetupScrimDashboardTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from commands import scrim
        self.scrim = scrim
        self.addCleanup(setattr, scrim, '_daily_reset_task', None)

    async def _setup(self, client, refresh_error=None):
        loop_coro = AsyncMock()
        tdm = MagicMock(scrim_day=6, scrim_month=10, is_team_assignment_started=False, teams={})
        with patch.object(self.scrim, 'daily_reset_loop', loop_coro), \
             patch.object(self.scrim, 'is_scrim_expired', return_value=False), \
             patch.object(self.scrim, '_refresh_scrim_dashboard', AsyncMock(side_effect=refresh_error)), \
             patch.object(self.scrim, 'BotManager') as BM:
            BM.get_instance.return_value.get_team_data_manager.return_value = tdm
            try:
                await self.scrim.setup_scrim_dashboard(client)
            finally:
                await asyncio.sleep(0)
        return loop_coro

    async def test_loop_starts_when_guild_missing(self):
        client = MagicMock()
        client.get_guild.return_value = None
        loop_coro = await self._setup(client)
        loop_coro.assert_awaited_once()
        client.get_guild.assert_called_once_with(settings.GUILD_ID)

    async def test_loop_starts_when_dashboard_refresh_fails(self):
        client = MagicMock()
        with self.assertRaises(RuntimeError):
            await self._setup(client, RuntimeError("send failed"))
        self.assertIsNotNone(self.scrim._daily_reset_task)
        await asyncio.sleep(0)
        self.assertTrue(self.scrim._daily_reset_task.done())


if __name__ == '__main__':
    unittest.main()
