import unittest
from unittest import mock

from models.team_data import TeamData
from utils.validators import compose_member_check_error


def make_manager():
    mgr = mock.Mock()
    mgr.is_maintenance = False
    mgr.check_team_time_rules.return_value = (True, "")
    mgr.check_member_restrictions = mock.AsyncMock(return_value=(True, ""))
    mgr.check_duplicate_with_bot_teams.return_value = (True, "")
    return mgr


def make_processor():
    processor = mock.Mock()
    processor.is_test_account.return_value = False
    return processor


class CombinedNicknameCheckTest(unittest.IsolatedAsyncioTestCase):
    def tearDown(self):
        mock.patch.stopall()

    async def _run(self, *, guild_missing, api_result):
        from commands import team_pipeline

        bot = mock.Mock()
        bot.get_client.return_value.get_guild.return_value = object()
        mock.patch.object(team_pipeline, "BotManager").start().get_instance.return_value = bot
        mock.patch.object(
            team_pipeline, "validate_members_in_guild",
            return_value=(not guild_missing, list(guild_missing)),
        ).start()
        api = mock.patch.object(
            team_pipeline, "validate_members_api", mock.AsyncMock(return_value=api_result)
        ).start()
        update = mock.patch.object(team_pipeline, "update_temp_message", mock.AsyncMock()).start()

        team = TeamData(name="알파팀", players=["가", "나", "다"])
        result = await team_pipeline._validate_team_rules(
            make_manager(), make_processor(), team, mock.Mock(), is_edit=False
        )
        message = update.call_args.args[1] if update.called else ""
        return result, message, api

    async def test_guild_and_game_failures_in_one_card(self):
        result, message, api = await self._run(guild_missing=["가"], api_result=(False, ["나"], False))

        self.assertEqual(result, (False, False))
        api.assert_awaited_once()
        self.assertIn("서버 별명과 다른 닉네임: **가**", message)
        self.assertIn("게임에서 찾지 못한 닉네임: **나**", message)
        self.assertIn("서버 별명과 게임 닉네임에 모두 맞아야", message)

    async def test_guild_failure_still_fails_during_maintenance(self):
        result, message, _ = await self._run(guild_missing=["가"], api_result=(True, [], True))

        self.assertEqual(result, (False, False))
        self.assertIn("**가**", message)
        self.assertNotIn("게임에서", message)

    async def test_maintenance_passes_when_guild_ok(self):
        result, message, _ = await self._run(guild_missing=[], api_result=(True, [], True))

        self.assertEqual(result, (True, True))
        self.assertEqual(message, "")

    async def test_game_server_unavailable_notice(self):
        result, message, _ = await self._run(guild_missing=["가"], api_result=(False, [], False))

        self.assertEqual(result, (False, False))
        self.assertIn("**가**", message)
        self.assertIn("게임 서버가 응답하지 않아", message)


class ComposeMemberCheckErrorTest(unittest.TestCase):
    def test_test_like_names_go_to_notice(self):
        message = compose_member_check_error(["가", "my test"], ["my test"])
        self.assertIn("서버 별명과 다른 닉네임: **가**", message)
        self.assertNotIn("게임에서 찾지 못한", message)
        self.assertEqual(message.count("my test"), 1)


class FailedInputDraftTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from commands import team_pipeline

        team_pipeline._failed_inputs.clear()

    def tearDown(self):
        mock.patch.stopall()

    def _patch_pipeline(self, *, passes: bool):
        from commands import team_pipeline

        bot = mock.Mock()
        mgr = bot.get_team_data_manager.return_value
        mgr.add_team = mock.AsyncMock(return_value=(True, ""))
        processor = bot.get_team_processor.return_value
        processor.ensure_test_accounts_loaded = mock.AsyncMock()
        processor.is_test_account.return_value = True
        mock.patch.object(team_pipeline, "BotManager").start().get_instance.return_value = bot
        mock.patch.object(
            team_pipeline, "_validate_team_rules", mock.AsyncMock(return_value=(passes, False))
        ).start()
        mock.patch.object(team_pipeline, "_fetch_team_mmr_or", mock.AsyncMock(return_value=0.0)).start()
        mock.patch.object(team_pipeline, "update_temp_message", mock.AsyncMock()).start()
        mock.patch.object(team_pipeline, "_save_user_cache").start()
        mock.patch.object(team_pipeline, "schedule_mmr_refresh").start()
        return team_pipeline

    async def _register(self, pipeline):
        interaction = mock.Mock()
        interaction.user.id = 7
        team = TeamData(name="알파팀", players=["가", "나", "다"], staff=["라"])
        await pipeline.process_team_registration(interaction, team, mock.Mock(), submitter=mock.Mock())

    async def test_failed_registration_is_kept_for_next_input(self):
        pipeline = self._patch_pipeline(passes=False)
        await self._register(pipeline)

        draft = pipeline.recall_failed_input("7")
        self.assertEqual(draft, {"team_name": "알파팀", "players": ["가", "나", "다"], "staff": ["라"]})
        self.assertIsNone(pipeline.recall_failed_input("7", original_team_name="알파팀"))
        self.assertIsNone(pipeline.recall_failed_input("8"))

    async def test_success_clears_draft(self):
        pipeline = self._patch_pipeline(passes=True)
        pipeline.remember_failed_input("7", TeamData(name="옛입력", players=["x"]))
        await self._register(pipeline)

        self.assertIsNone(pipeline.recall_failed_input("7"))

    async def test_draft_expires(self):
        from commands import team_pipeline

        with mock.patch.object(team_pipeline.time, "monotonic", return_value=1000.0):
            team_pipeline.remember_failed_input("7", TeamData(name="알파팀", players=["가"]))
        later = 1000.0 + team_pipeline.FAILED_INPUT_TTL_SECONDS + 1
        with mock.patch.object(team_pipeline.time, "monotonic", return_value=later):
            self.assertIsNone(team_pipeline.recall_failed_input("7"))
        self.assertNotIn("7", team_pipeline._failed_inputs)

    async def test_edit_draft_only_for_same_team(self):
        from commands import team_pipeline

        team_pipeline.remember_failed_input(
            "7", TeamData(name="새이름", players=["가"]), original_team_name="알파팀"
        )
        self.assertEqual(team_pipeline.recall_failed_input("7", original_team_name="알파팀")["team_name"], "새이름")
        self.assertIsNone(team_pipeline.recall_failed_input("7", original_team_name="베타팀"))
        self.assertIsNone(team_pipeline.recall_failed_input("7"))


class ModalGuidanceTest(unittest.IsolatedAsyncioTestCase):
    async def test_member_inputs_explain_name_rule(self):
        from commands.ui.modals import TeamModal

        components = TeamModal(mock.Mock()).to_components()
        labels = [c for c in components if c.get("type") == 18]
        self.assertEqual(len(labels), 2)
        for label in labels:
            self.assertIn("서버 별명과 게임 닉네임", label["description"])
            self.assertLessEqual(len(label["description"]), 100)

    async def test_registration_input_prefers_failed_draft(self):
        from commands import team_pipeline
        from commands.ui.views import TeamInputView
        from tests.test_pipeline_cancel import FakeInteraction

        team_pipeline._failed_inputs.clear()
        team_pipeline.remember_failed_input("7", TeamData(name="실패팀", players=["가", "나"]))
        mgr = mock.Mock()
        mgr.check_team_time_rules.return_value = (True, "")
        mgr.find_user_team.return_value = None
        bot = mock.Mock()
        bot.get_team_data_manager.return_value = mgr
        with mock.patch("commands.ui.views.BotManager") as bm, \
                mock.patch("commands.ui.views.check_cooldown", mock.AsyncMock(return_value=False)):
            bm.get_instance.return_value = bot
            inter = FakeInteraction(user_id=7)
            inter.response.send_modal = mock.AsyncMock()
            await TeamInputView(scrim_day=6, scrim_month=10, scrim_weekday="화요일").add_team_callback(inter)

        modal = inter.response.send_modal.call_args.args[0]
        self.assertEqual(modal.team_name_input.default, "실패팀")
        self.assertEqual(modal.players_input.default, "가\n나")
        team_pipeline._failed_inputs.clear()


if __name__ == "__main__":
    unittest.main()
