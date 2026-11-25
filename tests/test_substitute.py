import os
import tempfile
import unittest
from datetime import datetime
from unittest import mock

from commands import team_pipeline
from models import role_requests
from models.role_requests import APPROVED
from models.team_data import TeamData
from utils.helpers import KST

NOW = datetime(2026, 11, 25, 19, 0, tzinfo=KST)


class SubstituteTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.original = TeamData(
            name="Team ER", players=["가", "나", "다", "라"], staff=["코치"], user_id="10", mmr=7000.0, is_seed=True,
            created_at=datetime(2026, 11, 24, 22, 30, tzinfo=KST),
        )
        self.bot = mock.Mock()
        self.manager = self.bot.get_team_data_manager.return_value
        self.manager.get_team_data.return_value = self.original
        self.manager.replace_team = mock.AsyncMock(return_value=(True, ""))
        self.bot.get_team_processor.return_value.ensure_test_accounts_loaded = mock.AsyncMock()
        self.fetch = mock.AsyncMock(return_value=7200.0)
        self.changed = mock.AsyncMock()
        self.recorded = mock.Mock()
        self.announced = mock.AsyncMock()
        manager_class = mock.patch.object(team_pipeline, "BotManager")
        manager_class.start().get_instance.return_value = self.bot
        self.addCleanup(manager_class.stop)
        for target in (
            mock.patch.object(team_pipeline, "_fetch_team_mmr_or", self.fetch),
            mock.patch.object(team_pipeline, "_update_changed_team", self.changed),
            mock.patch.object(team_pipeline, "_record_roster_change", self.recorded),
            mock.patch.object(team_pipeline, "update_group_announcement", self.announced),
            mock.patch.object(team_pipeline, "get_current_kst_time", return_value=NOW),
        ):
            target.start()
            self.addCleanup(target.stop)

    async def _substitute(self):
        return await team_pipeline.substitute_players(mock.Mock(), "A", "Team ER", ["나", "라"], ["대타닉"])

    async def test_absent_players_leave_and_the_sub_joins_the_roster(self):
        self.assertEqual(await self._substitute(), "")

        name, saved, mmr = self.manager.replace_team.await_args.args
        self.assertEqual((name, mmr), ("Team ER", 7200.0))
        self.assertEqual(self.manager.replace_team.await_args.kwargs, {'enforce_rules': False})
        self.assertEqual(saved.players, ["가", "다", "대타닉"])
        self.assertEqual(
            (saved.staff, saved.user_id, saved.is_seed, saved.created_at, saved.updated_at),
            (["코치"], "10", True, self.original.created_at, NOW),
        )
        self.assertEqual(self.original.players, ["가", "나", "다", "라"])
        self.changed.assert_awaited_once_with("A", self.manager, "Team ER", "Team ER", 7200.0, grant_roles=False)
        self.recorded.assert_called_once()
        self.announced.assert_awaited_once_with("A")
        self.assertEqual(self.manager.log_action.call_args.kwargs['change'], "-나, 라 +대타닉")

    async def test_roster_changes_with_the_old_mmr_when_the_lookup_fails(self):
        self.fetch.return_value = 0.0

        self.assertEqual(
            await self._substitute(), "MMR을 조회하지 못했습니다.\n로스터 변경에서 닉네임을 확인해주세요: 대타닉",
        )

        self.assertEqual(self.manager.replace_team.await_args.args[2], 7000.0)
        self.announced.assert_awaited_once_with("A")

    async def test_roster_change_saved_by_an_operator_meanwhile_is_kept(self):
        async def fetch(*_):
            self.manager.get_team_data.return_value = TeamData(name="Team ER", players=["가", "나", "다", "마"])
            return 7200.0

        self.fetch.side_effect = fetch

        self.assertEqual(await self._substitute(), team_pipeline.ROSTER_NOT_CHANGED_TEXT)

        self.manager.replace_team.assert_not_awaited()

    async def test_roster_without_the_absent_player_is_not_changed(self):
        self.original.players = ["가", "다", "라", "마"]

        self.assertEqual(await self._substitute(), team_pipeline.ROSTER_NOT_CHANGED_TEXT)

        self.fetch.assert_not_awaited()
        self.manager.replace_team.assert_not_awaited()

    async def test_operator_is_told_when_the_roster_is_not_changed(self):
        for prepare in (
            lambda: setattr(self.manager.get_team_data, "return_value", None),
            lambda: setattr(self.manager.replace_team, "return_value", (False, "취소된 팀")),
            lambda: setattr(self.manager.replace_team, "side_effect", OSError("disk full")),
        ):
            self.manager.get_team_data.return_value = self.original
            prepare()

            self.assertEqual(await self._substitute(), team_pipeline.ROSTER_NOT_CHANGED_TEXT)

        self.announced.assert_not_awaited()


class GroupRoleSyncTest(unittest.IsolatedAsyncioTestCase):
    async def _update(self, **options):
        team = TeamData(name="Team ER", players=["가", "다", "대타닉"])
        bot = mock.Mock()
        manager = bot.get_team_data_manager.return_value
        manager.groups = [[("Team ER", TeamData(name="Team ER", players=["가", "나", "다"]), 7000.0)]]
        manager.teams = {"Team ER": team}
        service = bot.get_team_processor.return_value.discord_service
        service.change_group_role = mock.AsyncMock()
        service.rename_group_voice_channel = mock.AsyncMock()
        with mock.patch.object(team_pipeline, "BotManager") as manager_class, \
                mock.patch.object(team_pipeline, "_get_group_teams", return_value=manager.groups[0]):
            manager_class.get_instance.return_value = bot
            await team_pipeline._update_changed_team("A", manager, "Team ER", "Team ER", 7200.0, **options)
        self.assertEqual(manager.groups[0], [("Team ER", team, 7200.0)])
        service.rename_group_voice_channel.assert_awaited_once()
        return service.change_group_role

    async def test_approval_grants_no_role_by_nickname(self):
        (await self._update(grant_roles=False)).assert_not_awaited()

    async def test_roster_change_by_an_operator_grants_the_role_to_the_players_who_joined(self):
        change = await self._update()

        self.assertEqual(change.await_args.args[1:], ("A", ["대타닉"], (), ["가", "다", "대타닉"]))

    async def test_replaced_team_loses_the_role(self):
        change = await self._update(revoke_previous=True)

        self.assertEqual(change.await_args.args[1:], ("A", ["대타닉"], {"가", "나", "다"}, ["가", "다", "대타닉"]))


class RosterChangeSanctionTest(unittest.IsolatedAsyncioTestCase):
    async def test_player_sanctioned_by_an_approved_request_is_not_sanctioned_again(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        original = TeamData(name="Team ER", players=["가", "나 ", "다"], user_id="10")
        bot = mock.Mock()
        bot.get_team_data_manager.return_value.replace_team = mock.AsyncMock(return_value=(True, ""))
        bot.get_team_data_manager.return_value.unverified_teams = set()
        bot.get_team_data_manager.return_value.find_reserve.return_value = None
        bot.get_team_processor.return_value.ensure_test_accounts_loaded = mock.AsyncMock()
        warnings = mock.AsyncMock(return_value="2명에게 주의를 부여했습니다.")
        interaction = mock.Mock()
        with mock.patch.object(role_requests, "PATH", os.path.join(folder.name, "role_requests.json")), \
                mock.patch.object(role_requests, "_state", None), \
                mock.patch.object(role_requests, "get_current_kst_time", return_value=NOW), \
                mock.patch.object(team_pipeline, "BotManager") as manager_class, \
                mock.patch.object(team_pipeline, "_fetch_team_mmr_or", mock.AsyncMock(return_value=0.0)), \
                mock.patch.object(team_pipeline, "_update_changed_team", mock.AsyncMock()), \
                mock.patch.object(team_pipeline, "_record_roster_change"), \
                mock.patch.object(team_pipeline, "_collect_roster_warnings", mock.AsyncMock(return_value=[])), \
                mock.patch.object(team_pipeline, "_send_edit_result", mock.AsyncMock()), \
                mock.patch.object(team_pipeline, "update_group_announcement", mock.AsyncMock()), \
                mock.patch.object(team_pipeline, "apply_roster_warnings", warnings):
            manager_class.get_instance.return_value = bot
            role_requests.add(500, {
                'kind': 'sub', 'date': '2026-11-25', 'team': 'Team ER', 'letter': 'A', 'members': [20],
                'out': ['나'], 'sanction': '주의', 'status': APPROVED,
            })
            # 제재 없이 승인한 신청의 선수는 로스터 변경에서 제재를 받을 수 있음
            role_requests.add(501, {
                'kind': 'sub', 'date': '2026-11-25', 'team': 'Team ER', 'letter': 'A', 'members': [21],
                'out': ['다'], 'sanction': None, 'status': APPROVED,
            })
            await team_pipeline.process_team_edit(
                interaction,
                group_letter="A",
                original_team_name="Team ER",
                original_team_data=original,
                new_team_data=TeamData(name="Team ER", players=["가", "다", "대타닉"]),
                temp_message=mock.Mock(),
                is_roster_change=True,
                apply_warning=True,
            )

        warnings.assert_awaited_once_with(interaction, "Team ER", ["가", "다"], "대타")


if __name__ == '__main__':
    unittest.main()
