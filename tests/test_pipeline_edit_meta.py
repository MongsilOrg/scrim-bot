import unittest
from unittest import mock

from commands.team_pipeline import _update_mmr_message_for_individual_team


class MmrChannelAfterEditTest(unittest.IsolatedAsyncioTestCase):
    async def test_uses_resolved_channel_without_mmr_message(self):
        mgr = mock.Mock()
        mgr.mmr_message = None
        channel = object()
        mgr.resolve_mmr_channel.return_value = channel
        mgr.update_mmr_message = mock.AsyncMock()

        await _update_mmr_message_for_individual_team(mgr)

        mgr.update_mmr_message.assert_awaited_once_with(channel)

    async def test_skips_when_no_channel(self):
        mgr = mock.Mock()
        mgr.resolve_mmr_channel.return_value = None
        mgr.update_mmr_message = mock.AsyncMock()

        await _update_mmr_message_for_individual_team(mgr)

        mgr.update_mmr_message.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()


class EditKeepsCreatedAtTest(unittest.IsolatedAsyncioTestCase):
    async def test_created_at_kept_and_updated_at_kst(self):
        from datetime import datetime

        from commands import team_pipeline
        from models.team_data import TeamData
        from utils.helpers import KST

        created = datetime(2026, 10, 5, 22, 30, tzinfo=KST)
        now = datetime(2026, 10, 6, 15, 0, tzinfo=KST)
        original = TeamData(name='알파팀', players=['a', 'b', 'c'], user_id='1', created_at=created)
        edited = TeamData(name='알파팀', players=['a', 'b', 'd'])

        bot = mock.Mock()
        mgr = bot.get_team_data_manager.return_value
        mgr.replace_team = mock.AsyncMock(return_value=(True, ''))
        mgr.unverified_teams = set()
        bot.get_team_processor.return_value.ensure_test_accounts_loaded = mock.AsyncMock()
        interaction = mock.Mock()
        interaction.user.id = 1
        with mock.patch.object(team_pipeline, 'BotManager') as bm, \
                mock.patch.object(team_pipeline, '_validate_inputs', mock.AsyncMock(return_value=True)), \
                mock.patch.object(team_pipeline, '_validate_team_rules', mock.AsyncMock(return_value=(True, False))), \
                mock.patch.object(team_pipeline, '_fetch_team_mmr_or', mock.AsyncMock(return_value=0.0)), \
                mock.patch.object(team_pipeline, '_save_user_cache'), \
                mock.patch.object(team_pipeline, '_send_edit_result', mock.AsyncMock()), \
                mock.patch.object(team_pipeline, '_update_mmr_message_for_individual_team', mock.AsyncMock()), \
                mock.patch.object(team_pipeline, 'get_current_kst_time', return_value=now):
            bm.get_instance.return_value = bot
            await team_pipeline.process_team_edit(
                interaction,
                original_team_name='알파팀',
                original_team_data=original,
                new_team_data=edited,
                temp_message=mock.Mock(),
                is_roster_change=False,
            )

        saved = mgr.replace_team.call_args.args[1]
        self.assertIs(saved, edited)
        self.assertEqual(saved.created_at, created)
        self.assertEqual(saved.updated_at, now)
        self.assertEqual(saved.user_id, '1')
