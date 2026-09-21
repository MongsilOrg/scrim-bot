import asyncio
import unittest
from datetime import datetime
from unittest import mock

from models.team_data import TeamData, TeamMmrResult, format_team_mmr
from models.team_data_manager import TeamDataManager
from services.image_generator import ImageGenerator


def make_manager(team: TeamData) -> TeamDataManager:
    mgr = TeamDataManager.__new__(TeamDataManager)
    mgr.teams = {team.name: team}
    mgr._teams_lock = asyncio.Lock()
    mgr._mmr_dirty = False
    return mgr


class ApplyTeamMmrTest(unittest.TestCase):
    def _apply(self, team: TeamData, result: TeamMmrResult) -> TeamDataManager:
        mgr = make_manager(team)
        asyncio.run(mgr.apply_team_mmr(team.name, result))
        return mgr

    def test_confirmed_result_overwrites_and_clears_failures(self):
        team = TeamData(name='팀', players=['a', 'b', 'c'])
        team.mmr = 7000.0
        team.mmr_confirmed = True
        team.mmr_failed_players = ['a']

        mgr = self._apply(team, TeamMmrResult(mmr=7500.0, confirmed=True))

        self.assertEqual(team.mmr, 7500.0)
        self.assertTrue(team.mmr_confirmed)
        self.assertEqual(team.mmr_failed_players, [])
        self.assertIsNotNone(team.mmr_updated_at)
        self.assertTrue(mgr._mmr_dirty)

    def test_unconfirmed_result_keeps_last_confirmed_value(self):
        team = TeamData(name='팀', players=['a', 'b', 'c'])
        team.mmr = 7000.0
        team.mmr_confirmed = True
        team.mmr_updated_at = datetime(2026, 6, 1, 12, 0)

        self._apply(team, TeamMmrResult(failed_players=('b',)))

        self.assertEqual(team.mmr, 7000.0)
        self.assertTrue(team.mmr_confirmed)
        self.assertEqual(team.mmr_failed_players, ['b'])
        self.assertEqual(team.mmr_updated_at, datetime(2026, 6, 1, 12, 0))

    def test_unconfirmed_result_leaves_never_confirmed_team_pending(self):
        team = TeamData(name='팀', players=['a', 'b', 'c'])

        self._apply(team, TeamMmrResult(failed_players=('a', 'b')))

        self.assertFalse(team.mmr_confirmed)
        self.assertEqual(team.mmr_failed_players, ['a', 'b'])
        self.assertIsNone(team.mmr_updated_at)

    def test_confirmed_zero_is_stored_as_a_real_value(self):
        team = TeamData(name='팀', players=['a', 'b', 'c'])

        self._apply(team, TeamMmrResult(mmr=0.0, confirmed=True))

        self.assertEqual(team.mmr, 0.0)
        self.assertTrue(team.mmr_confirmed)
        self.assertEqual(team.mmr_display, '0.00')

    def test_repeated_failure_does_not_mark_dirty(self):
        team = TeamData(name='팀', players=['a', 'b', 'c'])
        team.mmr_failed_players = ['b']

        mgr = self._apply(team, TeamMmrResult(failed_players=('b',)))

        self.assertFalse(mgr._mmr_dirty)


class UpdateAllTeamMmrCountingTest(unittest.IsolatedAsyncioTestCase):
    async def _run(self, result: TeamMmrResult):
        from models.mmr_updater import MmrUpdater

        team = TeamData(name='팀A', players=['a', 'b', 'c'])
        mgr = make_manager(team)
        mgr.save_backup = mock.MagicMock()
        updater = MmrUpdater(mgr)

        tp = mock.MagicMock()
        tp.ensure_test_accounts_loaded = mock.AsyncMock()
        tp.fetch_team_mmr = mock.AsyncMock(return_value=('팀A', team, result))
        with mock.patch('models.mmr_updater.BotManager') as BM:
            BM.get_instance.return_value.get_team_processor.return_value = tp
            success, fail = await updater.update_all_team_mmr()
        return team, success, fail

    async def test_real_zero_team_counts_as_success(self):
        team, success, fail = await self._run(TeamMmrResult(mmr=0.0, confirmed=True))
        self.assertEqual((success, fail), (1, 0))
        self.assertTrue(team.mmr_confirmed)

    async def test_unconfirmed_team_counts_as_failure_and_records_members(self):
        team, success, fail = await self._run(TeamMmrResult(failed_players=('b',)))
        self.assertEqual((success, fail), (0, 1))
        self.assertEqual(team.mmr_failed_players, ['b'])


class TeamRowHtmlTest(unittest.TestCase):
    def _row(self, team: TeamData, is_unverified: bool = False) -> str:
        return ImageGenerator._build_team_row_html(1, team.name, team, is_unverified=is_unverified)

    def test_pending_team_shows_label_instead_of_zero(self):
        team = TeamData(name='팀', players=['a', 'b', 'c'])
        html = self._row(team)
        self.assertIn('<td class="mmr-pending">미확정</td>', html)
        self.assertNotIn('0.00', html)

    def test_confirmed_zero_team_shows_the_number(self):
        team = TeamData(name='팀', players=['a', 'b', 'c'])
        team.mmr_confirmed = True
        html = self._row(team)
        self.assertIn('<td class="mmr-value">0.00</td>', html)
        self.assertNotIn('미확정', html)

    def test_failed_members_are_greyed_out(self):
        team = TeamData(name='팀', players=['가나다', '라마바', '사아자'])
        team.mmr = 7000.0
        team.mmr_confirmed = True
        team.mmr_failed_players = ['라마바']
        html = self._row(team)
        self.assertIn('<span class="player-stale">라마바</span>', html)
        self.assertNotIn('<span class="player-stale">가나다</span>', html)
        self.assertIn('<td class="mmr-value">7000.00</td>', html)

    def test_failed_member_name_is_escaped(self):
        team = TeamData(name='팀', players=['<b>a</b>'])
        team.mmr_failed_players = ['<b>a</b>']
        html = self._row(team)
        self.assertIn('<span class="player-stale">&lt;b&gt;a&lt;/b&gt;</span>', html)

    def test_unverified_team_keeps_maintenance_label(self):
        team = TeamData(name='팀', players=['a'])
        html = self._row(team, is_unverified=True)
        self.assertIn('<td class="mmr-unverified">점검</td>', html)


class TeamDataSerializationTest(unittest.TestCase):
    def test_round_trip_keeps_mmr_status(self):
        team = TeamData(name='팀', players=['a', 'b', 'c'])
        team.mmr = 0.0
        team.mmr_confirmed = True
        team.mmr_failed_players = ['b']

        restored = TeamData.from_dict('팀', team.to_dict())

        self.assertTrue(restored.mmr_confirmed)
        self.assertEqual(restored.mmr_failed_players, ['b'])

    def test_legacy_backup_treats_positive_mmr_as_confirmed(self):
        restored = TeamData.from_dict('팀', {'players': ['a'], 'mmr': 7000.0})
        self.assertTrue(restored.mmr_confirmed)
        self.assertEqual(restored.mmr_failed_players, [])

    def test_legacy_backup_treats_zero_mmr_as_pending(self):
        restored = TeamData.from_dict('팀', {'players': ['a'], 'mmr': 0.0})
        self.assertFalse(restored.mmr_confirmed)


class FormatTeamMmrTest(unittest.TestCase):
    def test_format_rules(self):
        self.assertEqual(format_team_mmr(7000.0, True), '7000.00')
        self.assertEqual(format_team_mmr(0.0, True), '0.00')
        self.assertEqual(format_team_mmr(0.0, False), '미확정')
        self.assertEqual(format_team_mmr(7000.0, False), '미확정')


if __name__ == '__main__':
    unittest.main()
