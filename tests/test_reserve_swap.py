import unittest
from datetime import datetime
from unittest import mock

from commands import team_pipeline
from models.team_data import TeamData
from utils.helpers import KST


class ReserveSwapTest(unittest.IsolatedAsyncioTestCase):
    async def test_roster_change_into_reserve_keeps_reserve_applicant(self):
        applied = datetime(2026, 11, 25, 12, 0, tzinfo=KST)
        cancelled = TeamData(name='취소팀', players=['a', 'b', 'c'], user_id='1')
        reserve = TeamData(name='예비팀', players=['s1', 's2', 's3'], user_id='2', created_at=applied)
        edited = TeamData(name='예비팀', players=['s1', 's2', 's3'])
        bot = mock.Mock()
        mgr = bot.get_team_data_manager.return_value
        mgr.replace_team = mock.AsyncMock(return_value=(True, ''))
        mgr.find_reserve.return_value = reserve
        mgr.unverified_teams = {'예비팀'}
        bot.get_team_processor.return_value.ensure_test_accounts_loaded = mock.AsyncMock()
        interaction = mock.Mock()
        interaction.user.id = 9
        with mock.patch.object(team_pipeline, 'BotManager') as bm, \
                mock.patch.object(team_pipeline, '_fetch_team_mmr_or', mock.AsyncMock(return_value=0.0)), \
                mock.patch.object(team_pipeline, '_save_user_cache'), \
                mock.patch.object(team_pipeline, '_update_changed_team', mock.AsyncMock()), \
                mock.patch.object(team_pipeline, '_record_roster_change'), \
                mock.patch.object(team_pipeline, '_collect_roster_warnings', mock.AsyncMock(return_value=[])), \
                mock.patch.object(team_pipeline, '_send_edit_result', mock.AsyncMock()), \
                mock.patch.object(team_pipeline, '_update_group_announcement', mock.AsyncMock()):
            bm.get_instance.return_value = bot
            await team_pipeline.process_team_edit(
                interaction,
                group_letter='B',
                original_team_name='취소팀',
                original_team_data=cancelled,
                new_team_data=edited,
                temp_message=mock.Mock(),
                is_roster_change=True,
            )

        mgr.find_reserve.assert_called_once_with('예비팀')
        self.assertEqual((edited.user_id, edited.created_at), ('2', applied))
        self.assertEqual(mgr.replace_team.await_args.kwargs, {'enforce_rules': False})
        mgr.mark_unverified.assert_called_once_with('예비팀')


if __name__ == '__main__':
    unittest.main()
