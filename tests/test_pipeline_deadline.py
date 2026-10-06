import unittest
from datetime import datetime
from unittest import mock

from config.settings import settings
from models.team_data_manager import TeamDataManager
from tests.test_pipeline_cancel import FakeInteraction, texts


def make_manager(*, started=False, scrim_day=6) -> TeamDataManager:
    mgr = TeamDataManager.__new__(TeamDataManager)
    mgr.is_team_assignment_started = started
    mgr.scrim_day = scrim_day
    mgr.scrim_month = 10
    mgr.teams = {}
    mgr.team_by_member = {}
    return mgr


class DeadlineBeforeInputTest(unittest.IsolatedAsyncioTestCase):
    def tearDown(self):
        mock.patch.stopall()

    async def _press(self, mgr, now):
        from commands.ui.views import TeamInputView

        bot = mock.Mock()
        bot.get_team_data_manager.return_value = mgr
        mock.patch("commands.ui.views.BotManager").start().get_instance.return_value = bot
        mock.patch("commands.ui.views.check_cooldown", mock.AsyncMock(return_value=False)).start()
        mock.patch("commands.ui.views.get_current_kst_time", return_value=now).start()
        inter = FakeInteraction()
        inter.response.send_modal = mock.AsyncMock()
        view = TeamInputView(scrim_day=6, scrim_month=10, scrim_weekday="화요일")
        await view.add_team_callback(inter)
        return inter

    async def test_after_deadline_input_does_not_open(self):
        now = datetime(2026, 10, 6, settings.TEAM_REGISTRATION_DEADLINE_HOUR, 1)
        inter = await self._press(make_manager(), now)

        inter.response.send_modal.assert_not_called()
        body = texts(inter.response.sent[0])
        self.assertIn(f"{settings.TEAM_REGISTRATION_DEADLINE_HOUR}시에 신청과 수정이 마감되었습니다.", body)
        self.assertIn(f"{settings.NEXT_SCRIM_OPEN_HOUR}시에 열립니다", body)
        self.assertNotIn("관리자", body)

    async def test_after_assignment_input_does_not_open(self):
        now = datetime(2026, 10, 6, 12, 0)
        inter = await self._press(make_manager(started=True), now)

        inter.response.send_modal.assert_not_called()
        self.assertIn("조편성이 끝나", texts(inter.response.sent[0]))

    async def test_before_deadline_input_opens(self):
        now = datetime(2026, 10, 6, settings.TEAM_REGISTRATION_DEADLINE_HOUR - 1, 59)
        inter = await self._press(make_manager(), now)

        inter.response.send_modal.assert_called_once()


if __name__ == "__main__":
    unittest.main()
