import unittest
from unittest import mock

from discord.ui import LayoutView

from models.team_data import TeamData


def texts(view):
    found = []

    def walk(item):
        content = getattr(item, "content", None)
        if isinstance(content, str):
            found.append(content)
        for child in getattr(item, "children", []) or []:
            walk(child)

    for item in view.children:
        walk(item)
    return "\n".join(found)


class FakeResponse:
    def __init__(self):
        self.edited = []
        self.sent = []
        self._done = False

    def is_done(self):
        return self._done

    async def edit_message(self, **kwargs):
        self._done = True
        self.edited.append(kwargs.get("view"))

    async def send_message(self, **kwargs):
        self._done = True
        self.sent.append(kwargs.get("view"))


class FakeInteraction:
    def __init__(self, user_id=1):
        self.user = mock.Mock(id=user_id)
        self.channel = None
        self.response = FakeResponse()
        self.original_edits = []

    async def edit_original_response(self, **kwargs):
        self.original_edits.append(kwargs.get("view"))

    async def original_response(self):
        return None


def make_confirm(on_confirm):
    from commands.ui.views import ConfirmView
    from discord import Color

    return ConfirmView(
        title="확인",
        body="본문",
        confirm_label="취소하기",
        confirm_emoji="⚠️",
        accent_colour=Color.orange(),
        error_text="오류가 발생했습니다.",
        on_confirm=on_confirm,
    )


class ConfirmViewTest(unittest.IsolatedAsyncioTestCase):
    async def test_confirm_replaces_card_with_result_and_stops(self):
        from utils.layout_helpers import success_view

        result = success_view("처리했습니다.")

        async def on_confirm(_):
            return result

        view = make_confirm(on_confirm)
        view.on_timeout = mock.AsyncMock()
        inter = FakeInteraction()

        await view.confirm_callback(inter)

        self.assertTrue(view.is_finished())
        self.assertEqual(inter.original_edits, [result])
        view.on_timeout.assert_not_called()

    async def test_confirm_error_replaces_card_with_error(self):
        async def on_confirm(_):
            raise RuntimeError("boom")

        view = make_confirm(on_confirm)
        inter = FakeInteraction()

        await view.confirm_callback(inter)

        self.assertEqual(len(inter.original_edits), 1)
        self.assertIn("오류가 발생했습니다.", texts(inter.original_edits[0]))

    async def test_confirm_without_result_keeps_disabled_card(self):
        async def on_confirm(_):
            return None

        view = make_confirm(on_confirm)
        inter = FakeInteraction()

        await view.confirm_callback(inter)

        self.assertTrue(view.is_finished())
        self.assertEqual(inter.original_edits, [])
        self.assertTrue(view.confirm_button.disabled)

    async def test_back_replaces_card_and_stops(self):
        async def on_confirm(_):
            raise AssertionError("호출되면 안 됨")

        view = make_confirm(on_confirm)
        inter = FakeInteraction()

        await view.back_callback(inter)

        self.assertTrue(view.is_finished())
        self.assertIn("취소하지 않았습니다.", texts(inter.response.edited[0]))


def patch_manager(teams):
    mgr = mock.Mock()
    mgr.is_team_assignment_started = False
    mgr.get_team_data.side_effect = lambda name: teams.get(name)
    mgr.get_team_mmr.return_value = 0.0
    mgr.find_user_team.return_value = next(iter(teams), None)

    async def remove_team(name):
        teams.pop(name, None)
        return True, ""

    mgr.remove_team.side_effect = remove_team
    bot = mock.Mock()
    bot.get_team_data_manager.return_value = mgr
    patcher = mock.patch("commands.ui.views.BotManager")
    patcher.start().get_instance.return_value = bot
    return mgr


class ApplicantOnlyCancelTest(unittest.IsolatedAsyncioTestCase):
    def tearDown(self):
        mock.patch.stopall()

    def _view(self):
        from commands.ui.views import TeamInputView

        return TeamInputView(scrim_day=6, scrim_month=10, scrim_weekday="화요일")

    async def test_member_who_is_not_applicant_gets_notice(self):
        teams = {"알파팀": TeamData(name="알파팀", players=["a", "b", "c"], user_id="1")}
        patch_manager(teams)
        mock.patch("commands.ui.views.check_cooldown", mock.AsyncMock(return_value=False)).start()
        inter = FakeInteraction(user_id=2)

        await self._view().cancel_team_callback(inter)

        self.assertIn("신청자만", texts(inter.response.sent[0]))
        self.assertIn("알파팀", teams)

    async def test_confirm_rechecks_applicant(self):
        teams = {"알파팀": TeamData(name="알파팀", players=["a", "b", "c"], user_id="1")}
        mgr = patch_manager(teams)
        mock.patch("commands.ui.views.schedule_mmr_refresh").start()

        result = await self._view()._process_team_cancellation(FakeInteraction(user_id=2), "알파팀")

        self.assertIn("신청자만", texts(result))
        mgr.remove_team.assert_not_called()

    async def test_applicant_cancel_returns_result_card(self):
        teams = {"알파팀": TeamData(name="알파팀", players=["a", "b", "c"], user_id="1")}
        patch_manager(teams)
        mock.patch("commands.ui.views.schedule_mmr_refresh").start()

        result = await self._view()._process_team_cancellation(FakeInteraction(user_id=1), "알파팀")

        self.assertIsInstance(result, LayoutView)
        self.assertIn("취소되었습니다", texts(result))
        self.assertNotIn("알파팀", teams)

    async def test_force_cancel_returns_result_card(self):
        teams = {"알파팀": TeamData(name="알파팀", players=["a", "b", "c"], user_id="1")}
        patch_manager(teams)
        mock.patch("commands.ui.views.schedule_mmr_refresh").start()
        mock.patch("commands.ui.views.is_admin", return_value=True).start()

        result = await self._view()._execute_force_cancel(FakeInteraction(user_id=9), "알파팀")

        self.assertIn("강제 취소했습니다", texts(result))
        self.assertNotIn("알파팀", teams)


if __name__ == "__main__":
    unittest.main()
