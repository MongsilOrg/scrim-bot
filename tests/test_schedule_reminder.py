import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

from commands import schedule

KST = ZoneInfo("Asia/Seoul")


def _member(uid, name):
    return SimpleNamespace(id=uid, display_name=name, mention=f"<@{uid}>")


class ScheduleReminderTest(unittest.IsolatedAsyncioTestCase):
    def test_next_reminder_is_sunday_22(self):
        tue = datetime(2026, 10, 6, 15, 0, tzinfo=KST)
        self.assertEqual(schedule._next_reminder(tue), datetime(2026, 10, 11, 22, 0, tzinfo=KST))
        sun_late = datetime(2026, 10, 11, 22, 30, tzinfo=KST)
        self.assertEqual(schedule._next_reminder(sun_late), datetime(2026, 10, 18, 22, 0, tzinfo=KST))

    async def _run(self, *, assignments=None, week_start_offset=timedelta(hours=3)):
        now = datetime(2026, 10, 11, 22, 0, tzinfo=KST)
        mgr = SimpleNamespace(
            assignments=assignments or {}, week_start=now + week_start_offset, week_label="10/12 ~ 10/17",
            get_responded_user_ids=lambda: {"1"},
        )
        channel = SimpleNamespace(send=AsyncMock())
        guild = SimpleNamespace(get_channel=lambda cid: channel)
        client = SimpleNamespace(get_guild=lambda gid: guild)
        with patch.object(schedule.BotManager, "get_instance", return_value=SimpleNamespace(get_schedule_manager=lambda: mgr)), \
             patch.object(schedule, "schedule_members", return_value=[_member(1, "a"), _member(2, "b"), _member(3, "c")]), \
             patch.object(schedule, "get_current_kst_time", return_value=now):
            sent = await schedule.send_schedule_reminder(client)
        return sent, channel.send

    async def test_mentions_only_non_responders(self):
        sent, send = await self._run()
        self.assertEqual(sent, 2)
        text = send.await_args.args[0]
        self.assertIn("<@2> <@3>", text)
        self.assertNotIn("<@1>", text)

    async def test_skips_after_assignment_or_when_week_not_open(self):
        self.assertEqual((await self._run(assignments={0: ["1"]}))[0], 0)
        self.assertEqual((await self._run(week_start_offset=-timedelta(days=1)))[0], 0)


class WeekOpenTest(unittest.TestCase):
    def test_week_opens_friday_night(self):
        mgr = SimpleNamespace(week_start=datetime(2026, 10, 5, tzinfo=KST))
        with patch.object(schedule, "get_current_kst_time", return_value=datetime(2026, 10, 9, 21, 59, tzinfo=KST)):
            self.assertFalse(schedule._should_auto_reset(mgr))
        with patch.object(schedule, "get_current_kst_time", return_value=datetime(2026, 10, 9, 22, 0, tzinfo=KST)):
            self.assertTrue(schedule._should_auto_reset(mgr))


if __name__ == "__main__":
    unittest.main()
