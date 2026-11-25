import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest import mock

from commands import team_pipeline
from commands.ui import roster_views
from models.team_data import TeamData
from tests.test_pipeline_concurrency import make_manager
from utils.helpers import KST


class ReserveSwapTest(unittest.IsolatedAsyncioTestCase):
    async def test_roster_change_into_reserve_keeps_reserve_applicant_and_mmr_fallback(self):
        applied = datetime(2026, 11, 25, 12, 0, tzinfo=KST)
        cancelled = TeamData(name='취소팀', players=['a', 'b', 'c'], user_id='1', mmr=7000.0)
        reserve = TeamData(name='예비팀', players=['s1', 's2', 's3'], user_id='2', created_at=applied, mmr=6000.0)
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
                mock.patch.object(team_pipeline, '_fetch_team_mmr_or', mock.AsyncMock(return_value=0.0)) as fetch, \
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
        self.assertEqual(fetch.await_args.args[2], 6000.0)

    async def test_substitute_request_takes_one_player_from_reserve(self):
        alpha = TeamData(name='알파팀', players=['a1', 'a2', 'a3'], mmr=7000.0)
        spare = TeamData(name='예비팀', players=['s1', 's2', 's3'], mmr=6000.0)
        mgr = make_manager({'알파팀': alpha, '예비팀': spare}, started=True)
        mgr.groups = [[('알파팀', alpha, 7000.0)]]
        bot = mock.Mock()
        bot.get_team_data_manager.return_value = mgr
        bot.get_team_processor.return_value.ensure_test_accounts_loaded = mock.AsyncMock()
        with mock.patch.object(team_pipeline, 'BotManager') as bm, \
                mock.patch.object(team_pipeline, '_fetch_team_mmr_or', mock.AsyncMock(return_value=6800.0)), \
                mock.patch.object(team_pipeline, '_log_edit_diff'), \
                mock.patch.object(team_pipeline, '_update_changed_team', mock.AsyncMock()), \
                mock.patch.object(team_pipeline, '_record_roster_change'), \
                mock.patch.object(team_pipeline, '_update_group_announcement', mock.AsyncMock()):
            bm.get_instance.return_value = bot
            failed = await team_pipeline.substitute_players(mock.Mock(), 'A', '알파팀', ['a3'], ['s1'])

        self.assertEqual(failed, '')
        self.assertEqual(mgr.teams['알파팀'].players, ['a1', 'a2', 's1'])
        self.assertEqual(mgr.teams['알파팀'].mmr, 6800.0)
        self.assertEqual((spare.players, spare.mmr), (['s1', 's2', 's3'], 6000.0))
        self.assertIs(mgr.find_reserve('예비팀'), spare)


class ReservePresetTest(unittest.IsolatedAsyncioTestCase):
    def _view(self):
        alpha = TeamData(name='알파팀', players=['a1', 'a2', 'a3'])
        self.spare = TeamData(name='예비팀', players=['s1', 's2', 's3'], staff=['코치'], mmr=6000.0)
        parent = SimpleNamespace(group_teams=[('알파팀', alpha, 7000.0)], group_letter='A')
        return roster_views.TeamSelectionView(parent, [self.spare])

    def _interaction(self):
        interaction = mock.Mock()
        interaction.response.is_done.return_value = False
        interaction.response.send_modal = mock.AsyncMock()
        interaction.response.edit_message = mock.AsyncMock()
        return interaction

    async def _pick_team(self, view, *, still_reserve=True):
        interaction = self._interaction()
        with mock.patch.object(roster_views.Select, 'values', new_callable=mock.PropertyMock, return_value=['알파팀']), \
                mock.patch.object(roster_views, 'BotManager') as bm, \
                mock.patch.object(roster_views, 'send_response', mock.AsyncMock()) as respond, \
                mock.patch.object(roster_views, 'TeamEditModal') as modal:
            manager = bm.get_instance.return_value.get_team_data_manager.return_value
            manager.find_reserve.return_value = self.spare if still_reserve else None
            await view.team_select_callback(interaction)
        if not still_reserve:
            interaction.response.send_modal.assert_not_awaited()
            return respond
        interaction.response.send_modal.assert_awaited_once()
        return modal.call_args.kwargs

    async def test_no_reserve_menu_without_reserves(self):
        parent = SimpleNamespace(group_teams=[('알파팀', TeamData(name='알파팀', players=['a1']), 0.0)])
        self.assertIsNone(roster_views.TeamSelectionView(parent).reserve_select)

    async def test_team_pick_without_reserve_opens_original_roster(self):
        self.assertEqual(await self._pick_team(self._view()), {'is_roster_change': True, 'draft': None})

    async def test_reserve_pick_fills_modal_with_reserve_roster(self):
        view = self._view()
        interaction = self._interaction()
        with mock.patch.object(roster_views.Select, 'values', new_callable=mock.PropertyMock, return_value=['예비팀']):
            await view.reserve_select_callback(interaction)

        interaction.response.edit_message.assert_awaited_once_with(view=view)
        self.assertEqual([option.default for option in view.reserve_select.options], [True])
        self.assertEqual((await self._pick_team(view))['draft'], {
            'team_name': '예비팀', 'players': ['s1', 's2', 's3'], 'staff': ['코치'],
        })

    async def test_reserve_cancelled_while_menu_open_does_not_fill_modal(self):
        view = self._view()
        view.reserve = self.spare

        respond = await self._pick_team(view, still_reserve=False)

        respond.assert_awaited_once()

    async def test_clearing_reserve_pick_restores_original_roster(self):
        view = self._view()
        view.reserve = self.spare
        with mock.patch.object(roster_views.Select, 'values', new_callable=mock.PropertyMock, return_value=[]):
            await view.reserve_select_callback(self._interaction())

        self.assertIsNone((await self._pick_team(view))['draft'])


if __name__ == '__main__':
    unittest.main()
