import unittest
from datetime import date
from unittest import mock

from models.team_data import TeamData
from tests.test_pipeline_concurrency import make_manager
from tests.test_pipeline_restriction_date import DateAwareWarningManager


class RosterWarningsTest(unittest.IsolatedAsyncioTestCase):
    def tearDown(self):
        mock.patch.stopall()

    async def test_collects_restricted_duplicate_and_missing(self):
        from commands import team_pipeline

        new_team = TeamData(name='새알파팀', players=['a1', 'b1', '제한됨'], staff=['없는사람'])
        mgr = make_manager({
            '새알파팀': new_team,
            '베타팀': TeamData(name='베타팀', players=['b1', 'b2', 'b3']),
        }, started=True)
        mgr.client = None

        class OnlyOneRestricted(DateAwareWarningManager):
            def is_restricted(self, target_id=None, target_name=None, check_date=None):
                if target_name == '제한됨':
                    return True, '2026-10-08'
                return False, None

        bot = mock.Mock()
        bot.get_warning_manager.return_value = OnlyOneRestricted(date(2026, 10, 8))
        bot.get_client.return_value.get_guild.return_value = object()
        mock.patch('models.team_data_manager.BotManager').start().get_instance.return_value = bot
        mock.patch.object(team_pipeline, 'BotManager').start().get_instance.return_value = bot
        mock.patch.object(team_pipeline, 'validate_members_in_guild', return_value=(False, ['없는사람'])).start()
        processor = mock.Mock()
        processor.is_test_account.return_value = False

        warnings = await team_pipeline._collect_roster_warnings(mgr, processor, new_team)

        joined = '\n'.join(warnings)
        self.assertIn('**제한됨** 10월 8일 목요일까지', joined)
        self.assertIn('**b1** 베타팀', joined)
        self.assertNotIn('a1', joined)
        self.assertIn('**없는사람**', joined)

    async def test_result_card_shows_warnings_and_sanction(self):
        from commands import team_pipeline

        update = mock.patch.object(team_pipeline, 'update_temp_message', mock.AsyncMock()).start()
        processor = mock.Mock()
        processor.is_test_account.return_value = False
        team = TeamData(name='알파팀', players=['a1', 'a2', 'b1'])

        await team_pipeline._send_edit_result(
            mock.Mock(), processor, '알파팀', team, 100.0, {'b1'}, {'a3'}, False,
            roster_warnings=['다른 팀과 중복: **b1** 베타팀'], sanction_line='주의 3명을 부여했습니다.',
        )

        body, color = update.call_args.args[1], update.call_args.args[2]
        self.assertIn('다른 팀과 중복', body)
        self.assertIn('주의 3명을 부여했습니다.', body)
        self.assertEqual(color, team_pipeline.discord.Color.orange())


class DuplicateMessageTest(unittest.TestCase):
    def test_lists_every_conflict(self):
        mgr = make_manager({
            '알파팀': TeamData(name='알파팀', players=['a1', 'a2', 'a3']),
            '베타팀': TeamData(name='베타팀', players=['b1', 'b2', 'b3']),
        })
        ok, msg = mgr.check_duplicate_with_bot_teams('감마팀', ['A1', 'b2', 'c'])
        self.assertFalse(ok)
        self.assertIn('**A1**: 알파팀', msg)
        self.assertIn('**b2**: 베타팀', msg)


if __name__ == '__main__':
    unittest.main()
