import unittest
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

from models.scrim_orchestrator import ScrimOrchestrator

# TeamDataManager에 실제로 있는 이름만, 호출부의 except가 AttributeError를 삼킴
MANAGER_ATTRS = [
    'teams', 'update_all_team_mmr', 'update_mmr_message',
    'resolve_mmr_channel', 'mark_mmr_success',
]


class TestRefreshMmrBeforeAssignment(unittest.IsolatedAsyncioTestCase):
    def _make_manager(self, mmr_result):
        mgr = MagicMock(spec=MANAGER_ATTRS)
        mgr.teams = {'팀A': object()}
        mgr.update_all_team_mmr = AsyncMock(return_value=mmr_result)
        mgr.update_mmr_message = AsyncMock()
        mgr.resolve_mmr_channel = MagicMock(return_value=object())
        return mgr

    async def test_refresh_marks_success_time_on_success(self):
        mgr = self._make_manager((3, 0))
        orch = ScrimOrchestrator(MagicMock())
        await orch._refresh_mmr_before_assignment(mgr)
        mgr.mark_mmr_success.assert_called_once()
        mgr.update_all_team_mmr.assert_awaited_once_with(force=True)
        mgr.update_mmr_message.assert_awaited_once()

    async def test_refresh_keeps_time_when_all_fail(self):
        mgr = self._make_manager((0, 5))
        orch = ScrimOrchestrator(MagicMock())
        await orch._refresh_mmr_before_assignment(mgr)
        mgr.mark_mmr_success.assert_not_called()
        mgr.update_mmr_message.assert_awaited_once()


class TestUpdateAllTeamMmrForce(unittest.IsolatedAsyncioTestCase):
    async def _run(self, force):
        from models.mmr_updater import MmrUpdater
        team = MagicMock()
        team.mmr_updated_at = datetime(2026, 6, 1, 16, 59)
        mgr = MagicMock(spec=['teams', 'set_team_mmr', 'save_backup'])
        mgr.teams = {'팀A': team}
        mgr.set_team_mmr = AsyncMock()
        mgr.save_backup = MagicMock()
        updater = MmrUpdater(mgr)

        tp = MagicMock()
        tp.ensure_test_accounts_loaded = AsyncMock()
        tp.fetch_team_mmr = AsyncMock(return_value=('x', 'y', 1500))
        with patch('models.mmr_updater.get_current_kst_time',
                   return_value=datetime(2026, 6, 1, 17, 0)), \
             patch('models.mmr_updater.BotManager') as BM:
            BM.get_instance.return_value.get_team_processor.return_value = tp
            success, fail = await updater.update_all_team_mmr(force=force)
        return tp.fetch_team_mmr.await_count, success, fail

    async def test_force_bypasses_10min_cache(self):
        calls, success, fail = await self._run(force=True)
        self.assertEqual(calls, 1)
        self.assertEqual((success, fail), (1, 0))

    async def test_no_force_skips_recent(self):
        calls, success, fail = await self._run(force=False)
        self.assertEqual(calls, 0)
        self.assertEqual(success, 1)


if __name__ == '__main__':
    unittest.main()
