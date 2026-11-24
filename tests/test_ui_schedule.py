import unittest
from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord

from commands.ui import schedule_views
from config.settings import settings
from models import schedule_manager
from models.schedule_manager import ScheduleManager
from utils.helpers import KST

MONDAY = datetime(2026, 11, 23, tzinfo=KST)


def _manager():
    mgr = ScheduleManager()
    mgr.week_start = MONDAY
    mgr.week_label = schedule_manager._week_label(MONDAY)
    mgr.admin_names = {"a": "Alice", "b": "Bob", "c": "Carol", "d": "Dan"}
    mgr.availability = {"a": {0, 1, 2, 3}, "b": {0, 1, 2}, "c": {2, 4}, "d": set()}
    mgr.absence_reasons = {"d": {-1: "출장"}}
    return mgr


def _status(mgr, today, groups=None):
    with patch.object(schedule_manager, "scrim_day", return_value=today):
        return mgr.get_status_text([(uid, name) for uid, name in mgr.admin_names.items()] + [("e", "Eve")], groups)


class WeekLabelTest(unittest.TestCase):
    def test_same_month_drops_second_month(self):
        self.assertEqual(schedule_manager._week_label(MONDAY), "11월 23일부터 27일까지")

    def test_month_change_keeps_second_month(self):
        self.assertEqual(schedule_manager._week_label(datetime(2026, 11, 30, tzinfo=KST)), "11월 30일부터 12월 4일까지")


class StatusTextTest(unittest.TestCase):
    def test_done_days_show_groups_and_remaining_days_show_ranked_names(self):
        mgr = _manager()
        mgr.actual_deployments = {0: ["a", "b"], 1: ["b", "a", "c"]}
        mgr.deploy_history = {"2026-11-23": ["a", "b"], "2026-11-24": ["b", "a", "c"]}

        text = _status(mgr, date(2026, 11, 25), {1: {"B": ["b"], "A": ["a", "x"]}})

        self.assertEqual(text, "\n".join([
            "## 주간 일정",
            "11월 23일부터 27일까지",
            "",
            "**진행**",
            "월 Alice, Bob",
            "화 A조 Alice, B조 Bob, Carol",
            "",
            "**참가**",
            "수 Carol, Bob, Alice",
            "목 Alice",
            "금 Carol",
            "",
            "**불참** Dan: 출장",
            "**미응답** Eve",
        ]))

    def test_past_day_without_record_is_left_out_and_empty_day_says_so(self):
        mgr = _manager()
        mgr.availability = {"a": {0, 4}}
        mgr.absence_reasons = {}

        text = _status(mgr, date(2026, 11, 26))

        self.assertNotIn("**진행**", text)
        self.assertIn("**참가**\n목 없음\n금 Alice", text)
        self.assertNotIn("**불참**", text)

    def test_rest_day_row(self):
        mgr = _manager()
        mgr.rest_days = {0, 3}

        text = _status(mgr, date(2026, 11, 24))

        self.assertIn("**진행**\n월 자율 진행", text)
        self.assertIn("목 자율 진행", text.split("**참가**")[1])

    def test_week_of_rest_days_has_no_missing_row(self):
        mgr = _manager()
        mgr.rest_days = {0, 1, 2, 3, 4}

        text = _status(mgr, date(2026, 11, 23))

        self.assertNotIn("**미응답**", text)
        self.assertIn("**불참** Dan: 출장", text)

    def test_everyone_answered_has_no_missing_row(self):
        mgr = _manager()
        with patch.object(schedule_manager, "scrim_day", return_value=date(2026, 11, 23)):
            text = mgr.get_status_text([("a", "Alice")])
        self.assertNotIn("**미응답**", text)


class ScheduleViewTest(unittest.IsolatedAsyncioTestCase):
    async def test_buttons_and_no_footer(self):
        view = schedule_views.ScheduleView("## 주간 일정")

        buttons = [item for item in view.walk_children() if isinstance(item, discord.ui.Button)]
        self.assertEqual([button.label for button in buttons], ["참가", "불참", "진행 기록"])
        self.assertTrue(all(button.emoji is None for button in buttons))
        self.assertEqual(buttons[2].url, f"{settings.LABS_URL}/er/scrim/schedule")
        texts = [item.content for item in view.walk_children() if isinstance(item, discord.ui.TextDisplay)]
        self.assertEqual(texts, ["## 주간 일정"])
        self.assertFalse([item for item in view.walk_children() if isinstance(item, discord.ui.Separator)])

    async def _press(self, mgr):
        interaction = MagicMock()
        interaction.user = SimpleNamespace(id=1, roles=[SimpleNamespace(id=777)])
        interaction.response.send_modal = AsyncMock()
        sent = AsyncMock()
        with patch.object(settings, "ADMIN_ROLE_IDS", [777]), \
                patch.object(schedule_views, "check_cooldown", new=AsyncMock(return_value=False)), \
                patch.object(schedule_views, "send_response", new=sent), \
                patch.object(schedule_views.BotManager, "get_instance",
                             return_value=SimpleNamespace(get_schedule_manager=lambda: mgr)):
            await schedule_views.ScheduleView("일정").register_callback(interaction)
        return interaction.response.send_modal, sent

    async def test_modal_leaves_out_rest_days(self):
        mgr = _manager()
        mgr.rest_days = {4}

        send_modal, _ = await self._press(mgr)

        checkbox = send_modal.await_args.args[0].days_checkbox
        self.assertEqual([option.value for option in checkbox.options], ["0", "1", "2", "3"])
        self.assertEqual(checkbox.max_values, 4)

    async def test_week_of_rest_days_opens_no_modal(self):
        mgr = _manager()
        mgr.rest_days = {0, 1, 2, 3, 4}

        send_modal, sent = await self._press(mgr)

        send_modal.assert_not_awaited()
        sent.assert_awaited_once()


class RefreshTest(unittest.IsolatedAsyncioTestCase):
    async def test_refresh_fills_rest_days_and_groups(self):
        mgr = _manager()
        mgr.actual_deployments = {1: ["a"]}
        mgr.save_backup = MagicMock()
        mgr.status_message_id, mgr.status_channel_id = 9, 5
        channel = SimpleNamespace(id=5)
        guild = SimpleNamespace(get_channel=lambda cid: channel)
        upsert = AsyncMock(return_value=9)

        async def rest_info(day):
            return {"is_rest_day": day == date(2026, 11, 27)}

        with patch.object(schedule_views, "schedule_members", return_value=[]), \
                patch.object(schedule_views, "get_rest_day_info", new=rest_info), \
                patch.object(schedule_views.operator_log, "deployed", return_value={"C": ["a"]}) as deployed, \
                patch.object(schedule_manager, "scrim_day", return_value=date(2026, 11, 25)), \
                patch.object(schedule_views, "upsert_persistent_message", new=upsert):
            await schedule_views.refresh_dashboard(guild, schedule_mgr=mgr)

        self.assertEqual(mgr.rest_days, {4})
        deployed.assert_called_once_with(date(2026, 11, 24))
        text = upsert.await_args.args[2].children[0].children[0].content
        self.assertIn("화 C조 Alice", text)
        self.assertIn("금 자율 진행", text)


if __name__ == "__main__":
    unittest.main()
